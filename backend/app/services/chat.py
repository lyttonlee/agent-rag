"""智能问答服务：Agent 编排（意图路由 → 策略分发）+ 多轮会话 + 检索增强 + 流式输出。

架构（详见《Agent 意图路由与分步执行方案 V1.1》）：
    IntentRouter 判定意图 → _CHAT_STRATEGIES 分发策略 → 各策略复用现有
    检索/生成能力 → 统一收尾（脱敏 → 落库 → done 事件）。

与公文起草（services/generation.py）的差异：
- 多轮：自动拼接会话历史（最近 N 轮）作为模型上下文，检索只用当前轮
  query（避免历史问题稀释召回精度，指代消解交给模型结合历史理解）；
- 会话持久化：user / assistant 消息落库，引用与敏感词命中随 assistant
  消息留痕，历史回看可核对依据；
- 用户点「停止」时 generator 被关闭（GeneratorExit），已生成部分自动
  保存为 assistant 消息，不丢答案。

策略矩阵（按意图分发，未知意图降级直答）：
- direct_qa：单轮检索 + 直答；**无材料时委婉拒答（固定话术，不编造）**；
- complex_task：Planner 拆步 → Executor 分步执行（过程可见）→ Aggregator
  汇总；无材料时改用模型自身能力完成，并追加甄别声明；
- draft：问答场景下的起草诉求按直答策略处理（检索素材 + 生成）；
- chitchat：不走检索，固定话术即时寒暄；
- out_of_scope：明确拒答 + 说明能力边界。

事件序列（SSE，向后兼容——老前端忽略不认识的 type 即可）：
  meta（conversation_id / intent / citations / no_result）
  plan（steps，仅 complex_task）
  step_start / step_end（仅 complex_task）
  token / reasoning
  done（answer + sensitive_hits + intent + no_result + disclaimer）
  error
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.db.models import ConfigKV, Conversation
from app.schemas.contracts import ChatRequest, Citation
from app.services import conversation as conv_service
from app.services.agent import (
    NO_RESULT_ANSWER,
    OUT_OF_SCOPE_ANSWER,
    TASK_DISCLAIMER,
    AgentConfig,
    IntentResult,
    IntentRouter,
    StepArtifact,
    build_final_messages,
    execute_plan,
    has_materials,
    intent_label,
    is_no_useful_info,
    load_agent_config,
    merge_citations,
    pick_chitchat_reply,
    plan_steps,
    stream_text,
)
from app.services.audit import record_audit
from app.services.generation_prompt import build_chat_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import hybrid_retrieve

logger = logging.getLogger(__name__)

# 多轮上下文：取最近 N 条消息（约 N/2 轮问答）
_HISTORY_LIMIT = 10

# 策略函数类型：吃上下文、吐 SSE 片段
_Strategy = Callable[["_ChatCtx"], AsyncGenerator[str, None]]


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@dataclass
class _ChatCtx:
    """单次问答的共享上下文：策略只写状态，收尾逻辑统一读状态。"""

    db: AsyncSession
    req: ChatRequest
    conversation_id: str
    history: list[tuple[str, str]]
    agent_cfg: AgentConfig
    intent: IntentResult
    actor: str
    agent_on: bool = True                 # Agent 是否开启（meta 事件如实回报）
    parts: list[str] = field(default_factory=list)      # 已生成正文片段
    citations: list[Citation] = field(default_factory=list)  # 最终引用（汇总合并后）
    saved: bool = False                                  # 助手消息落库幂等标志
    model_name: str = ""
    no_result: bool = False       # 未检索到有用信息（直接拒答 / 任务型兜底）
    disclaimer: bool = False      # 任务型无材料兜底 → 答案需追加甄别声明


async def _ensure_conversation(db: AsyncSession, req: ChatRequest) -> Conversation:
    """确保会话存在：缺省则按知识空间创建；已存在则同步空间绑定。"""
    if req.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.id == req.conversation_id)
            )
        ).scalar_one_or_none()
        if conv:
            # 请求显式带了 space_ids 时，同步更新会话绑定（换知识范围继续聊）
            if req.space_ids is not None and (conv.space_ids or []) != req.space_ids:
                conv.space_ids = req.space_ids
                await db.commit()
                await db.refresh(conv)
            return conv
        # 会话不存在（如前端误传）：静默新建，保证问答不中断
        logger.warning("会话 %s 不存在，自动创建新会话", req.conversation_id)
    return await conv_service.create_conversation(
        db, conv_type="chat", space_ids=req.space_ids or []
    )


def _resolve_space_ids(req: ChatRequest, conv_space_ids: list[str] | None) -> list[str] | None:
    """确定本次检索的空间范围：请求级 > 会话绑定 > 不限（None）。"""
    if req.space_ids:
        return req.space_ids
    if conv_space_ids:
        return conv_space_ids
    return None


async def _load_sensitive_words(db: AsyncSession, req: ChatRequest) -> list[str]:
    """敏感词表：请求级覆盖 > 配置中心词库（与生成链路一致）。"""
    if req.sensitive_words:
        return req.sensitive_words
    rows = (
        await db.execute(
            select(ConfigKV.key).where(
                ConfigKV.category == "sensitive_word",
                ConfigKV.is_active.is_(True),
            )
        )
    ).scalars().all()
    return [w for w in rows if w]


# ---------------------------------------------------------------- 策略实现

def _meta_event(ctx: _ChatCtx, model: str, citations: list[Citation], *, no_result: bool) -> dict:
    """构造 meta 事件（会话 ID + 意图 + 引用 + 无材料标志）。"""
    return {
        "type": "meta",
        "conversation_id": ctx.conversation_id,
        "model": model,
        "intent": ctx.intent.intent,
        "intent_label": intent_label(ctx.intent.intent),
        "agent_mode": ctx.agent_on,
        "no_result": no_result,
        "citations": [
            {
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "title": c.title,
                "doc_no": c.doc_no,
                "snippet": c.snippet,
                "score": c.score,
            }
            for c in citations
        ],
    }


async def _stream_model(ctx: _ChatCtx, messages: list[dict], gen: ModelProvider) -> AsyncGenerator[str, None]:
    """流式生成（思考链 + 正文），正文片段累积到 ctx.parts。"""
    async for kind, text in gen.generate_stream(messages):
        if kind == "content":
            ctx.parts.append(text)
            yield _sse({"type": "token", "text": text})
        else:
            yield _sse({"type": "reasoning", "text": text})


async def _run_direct_answer(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """direct_qa / draft（问答场景）：检索 + 直答；无有用信息 → 委婉拒答不编造。"""
    retrieve_resp = await hybrid_retrieve(ctx.db, ctx.req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations
    ctx.citations[:] = citations
    # "无有用信息"双信号判定（向量距离 + 关键词命中，见 policies.is_no_useful_info）
    ctx.no_result = is_no_useful_info(retrieve_resp)

    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name
    yield _sse(_meta_event(ctx, gen.model_name, citations, no_result=ctx.no_result))

    if ctx.no_result:
        # 硬性要求（用户 2026-10-13）：确切回答型意图 + 无有用检索结果 →
        # 固定话术委婉说明没有相关内容，不调用模型，从机制上杜绝编造。
        async for chunk in stream_text(NO_RESULT_ANSWER):
            ctx.parts.append(chunk)
            yield _sse({"type": "token", "text": chunk})
        return

    messages = build_chat_messages(ctx.req.query, ctx.history, citations)
    async for evt in _stream_model(ctx, messages, gen):
        yield evt


async def _run_step_plan(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """complex_task：拆步 → 分步执行（过程可见）→ 汇总。

    无材料时任务不失败：改用模型自身能力完成（intents.py 中 complex_task
    的 allows_self_capability=True），答案追加甄别声明。
    """
    plan = await plan_steps(ctx.db, ctx.req.query, ctx.agent_cfg)
    # 审计：任务计划留痕（步骤数/拆解通道可按意图统计用量）
    await record_audit(
        actor=ctx.actor,
        actor_ip=None,
        method="POST",
        endpoint="/v1/chat/stream",
        status_code=200,
        object_type="conversation",
        object_id=ctx.conversation_id,
        action="任务计划",
        result="success",
        detail=f"steps={len(plan.steps)} channel={plan.channel} query={ctx.req.query[:80]}",
    )
    yield _sse({
        "type": "plan",
        "steps": [
            {"id": s.id, "action": s.action, "query": s.query, "status": s.status}
            for s in plan.steps
        ],
        "channel": plan.channel,
    })

    artifacts: list[StepArtifact] = []
    async for evt in execute_plan(ctx.db, ctx.req, plan.steps, artifacts):
        yield _sse(evt)

    merged = merge_citations(artifacts)
    ctx.citations[:] = merged
    no_materials = not has_materials(artifacts)
    ctx.no_result = no_materials
    ctx.disclaimer = no_materials  # 无材料兜底：需向用户注明自行甄别

    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name
    yield _sse(_meta_event(ctx, gen.model_name, merged, no_result=no_materials))

    messages = build_final_messages(ctx.req.query, ctx.history, artifacts, merged)
    async for evt in _stream_model(ctx, messages, gen):
        yield evt


async def _run_chitchat(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """chitchat：不走检索，固定话术即时寒暄（省 token、零延迟、零编造）。"""
    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name
    yield _sse(_meta_event(ctx, gen.model_name, [], no_result=False))
    reply = pick_chitchat_reply(ctx.req.query)
    async for chunk in stream_text(reply):
        ctx.parts.append(chunk)
        yield _sse({"type": "token", "text": chunk})


async def _run_refuse(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """out_of_scope：明确拒答 + 说明能力边界，不编造。"""
    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name
    ctx.no_result = True  # 拒答属于"无有用信息"分支：前端展示能力边界提示
    yield _sse(_meta_event(ctx, gen.model_name, [], no_result=True))
    async for chunk in stream_text(OUT_OF_SCOPE_ANSWER):
        ctx.parts.append(chunk)
        yield _sse({"type": "token", "text": chunk})


# 意图 → 策略分发表。扩展新意图时：intents.py 注册 + 此处加一行即可，
# 编排骨架（stream_chat）零改动；未登记意图自动降级为直答策略。
_CHAT_STRATEGIES: dict[str, _Strategy] = {
    "direct_qa": _run_direct_answer,
    # 问答场景下的起草诉求：仍走检索+生成（起草的完整策略在 /v1/generate）
    "draft": _run_direct_answer,
    "complex_task": _run_step_plan,
    "chitchat": _run_chitchat,
    "out_of_scope": _run_refuse,
}


async def stream_chat(db: AsyncSession, req: ChatRequest, actor: str = "anonymous"):
    """智能问答流式生成器（SSE 文本片段）。

    Args:
        db: 数据库会话。
        req: 问答请求（agent_mode 缺省时读配置中心开关）。
        actor: 操作者身份（上游网关下发，用于意图识别审计留痕）。
    """
    # 1) 会话收敛 + 空间范围确定（请求级 > 会话绑定 > 不限）
    conv = await _ensure_conversation(db, req)
    conversation_id = conv.id
    space_ids = _resolve_space_ids(req, conv.space_ids or [])
    # 把收敛后的空间范围写回请求对象：hybrid_retrieve 从 req.space_ids 读取，
    # 否则"会话绑定空间"对检索不生效（请求未显式带 space_ids 时为 None）。
    req.space_ids = space_ids

    # 2) 用户消息落库（先落库再生成：即使生成失败，问题也留痕可追溯）
    await conv_service.append_message(db, conversation_id, "user", req.query)

    # 3) 多轮历史（刚写入的本轮 user 消息不重复进入上下文）
    history_rows = await conv_service.recent_history(db, conversation_id, limit=_HISTORY_LIMIT)
    history = [
        (m.role, m.content)
        for m in history_rows
        if not (m.role == "user" and m.content == req.query)
    ]

    # 4) Agent 配置与意图识别（agent_mode 关闭时行为与旧版完全一致）
    agent_cfg = await load_agent_config(db)
    agent_on = req.agent_mode if req.agent_mode is not None else agent_cfg.enabled
    if agent_on:
        intent_result = await IntentRouter(agent_cfg).classify(db, req.query)
        # 审计：意图识别留痕（可按意图统计用量、复盘误判）
        await record_audit(
            actor=actor,
            actor_ip=None,
            method="POST",
            endpoint="/v1/chat/stream",
            status_code=200,
            object_type="conversation",
            object_id=conversation_id,
            action="意图识别",
            result="success",
            detail=(
                f"intent={intent_result.intent} channel={intent_result.channel} "
                f"confidence={intent_result.confidence:.2f} "
                f"reason={intent_result.reason} query={req.query[:80]}"
            ),
        )
    else:
        intent_result = IntentResult("direct_qa", 1.0, "disabled", "Agent 已关闭，按直答处理")

    ctx = _ChatCtx(
        db=db,
        req=req,
        conversation_id=conversation_id,
        history=history,
        agent_cfg=agent_cfg,
        intent=intent_result,
        actor=actor,
        agent_on=agent_on,
    )
    strategy = _CHAT_STRATEGIES.get(intent_result.intent, _run_direct_answer)

    async def _save_assistant(answer: str, hits: list[str], model: str):
        """落库助手消息（幂等由 ctx.saved 保证；答案为空则不落库）。"""
        if ctx.saved or not answer.strip():
            return
        await conv_service.append_message(
            db,
            conversation_id,
            "assistant",
            answer,
            citations=[
                {
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "title": c.title,
                    "doc_no": c.doc_no,
                    "snippet": c.snippet,
                    "score": c.score,
                }
                for c in ctx.citations
            ],
            sensitive_hits=hits,
            model=model,
        )
        ctx.saved = True

    try:
        # 5) 策略执行（meta / plan / step_* / token 事件由策略内部产出）
        async for chunk in strategy(ctx):
            yield chunk

        # 6) 统一收尾：甄别声明 → 脱敏 → 完整答案 → 落库 → done
        raw = "".join(ctx.parts)
        if ctx.disclaimer and raw.strip():
            # 任务型无材料兜底：答案末尾追加"需自行甄别"声明（用户硬性要求），
            # 同时以 token 事件补推，保证流式视图与最终答案一致。
            raw += TASK_DISCLAIMER
            yield _sse({"type": "token", "text": TASK_DISCLAIMER})
        sensitive_words = await _load_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        await _save_assistant(answer, hits, ctx.model_name or "unknown")
        yield _sse({
            "type": "done",
            "conversation_id": conversation_id,
            "answer": answer,
            "sensitive_hits": hits,
            "model": ctx.model_name,
            "intent": ctx.intent.intent,
            "no_result": ctx.no_result,
            "disclaimer": ctx.disclaimer,
        })
    except GeneratorExit:
        # 用户点「停止」：保存已生成部分（未脱敏——停止时内容可能不完整，
        # 落库原样留痕；完整答案仅在 done 时脱敏）
        logger.info("问答生成被中止，保存部分答案（conversation=%s）", conversation_id)
        await _save_assistant("".join(ctx.parts), [], "unknown")
        raise
    except Exception as exc:
        logger.exception("智能问答异常：%s", exc)
        # 失败也尽量保存已生成部分，便于用户继续
        try:
            await _save_assistant("".join(ctx.parts), [], "unknown")
        except Exception:  # pragma: no cover - 兜底不影响主错误上报
            pass
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
