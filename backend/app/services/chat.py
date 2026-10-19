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
- deep_reason（2026-10-16 新增）：ReAct TAO 循环多轮检索（Thought →
  Action → Observation 动态决策，**不是计划定死的分步**）→ 多轮材料
  综合 + 交叉验证 → 最终生成。deep_on 时编排层把 direct_qa /
  complex_task 动态改写为该意图接管；关闭则行为与旧版完全一致；
- draft：问答场景下的起草/创作诉求按直答策略处理（有素材则检索+生成）；
  **任务型意图无材料时不拒答**——按意图规格 allows_self_capability 分流：
  确切回答型拒答，任务型用自身能力完成并追加甄别声明；
- chitchat：不走检索，固定话术即时寒暄；
- out_of_scope：明确拒答 + 说明能力边界。

思考链留痕：reasoning 事件内容累积并随助手消息落库；深度推理额外把
TAO 全链文本（每轮思考/行动/观察/停止原因）写入 reasoning，前端以
"默认折叠、可展开"面板展示，回答结束后不再随正文出现而消失。

事件序列（SSE，向后兼容——老前端忽略不认识的 type 即可）：
  meta（conversation_id / intent / citations / no_result）
  plan（steps，仅 complex_task）
  step_start / step_end（仅 complex_task）
  react_start / analysis（仅 deep_reason）
  react_round / thought / action / observation / react_end（仅 deep_reason）
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
    get_spec,
    has_materials,
    intent_label,
    is_no_useful_info,
    load_agent_config,
    merge_citations,
    pick_chitchat_reply,
    plan_steps,
    stream_text,
)
from app.services.agent.react_config import load_react_config
from app.services.agent.react_engine import ReactRunResult, run_react
from app.services.agent.react_prompt import build_final_messages as build_react_final_messages
from app.services.audit import record_audit
from app.services.auth import Caller
from app.services.generation_prompt import build_chat_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import retrieve_enhanced
from app.services.runtime_config import (
    RetrievalConfig,
    load_prompt_templates,
    load_retrieval_config,
)

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
    # 调用方（doc_lookup 工具的空间可见性判定用；内部调用可为 None）
    caller: Caller | None = None
    parts: list[str] = field(default_factory=list)      # 已生成正文片段
    reasoning_parts: list[str] = field(default_factory=list)  # 思考链片段（回答后可展开查看）
    citations: list[Citation] = field(default_factory=list)  # 最终引用（汇总合并后）
    saved: bool = False                                  # 助手消息落库幂等标志
    model_name: str = ""
    no_result: bool = False       # 未检索到有用信息（直接拒答 / 任务型兜底）
    disclaimer: bool = False      # 任务型无材料兜底 → 答案需追加甄别声明
    # 配置中心运行时配置（检索后按需加载；供提示词模板与阈值消费）
    retrieval_cfg: RetrievalConfig | None = None
    prompt_templates: dict | None = None

    @property
    def reasoning_text(self) -> str:
        """思考链全文（落库 / 前端折叠面板展示用）。"""
        return "".join(self.reasoning_parts)


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
    """流式生成（思考链 + 正文），正文片段累积到 ctx.parts、思考链累积到 ctx.reasoning_parts。

    思考链同样随助手消息落库（ctx.reasoning_text）：回答结束后前端以
    "默认折叠、可展开"面板展示，不再一出现正文就消失。
    """
    async for kind, text in gen.generate_stream(messages):
        if kind == "content":
            ctx.parts.append(text)
            yield _sse({"type": "token", "text": text})
        else:
            ctx.reasoning_parts.append(text)
            yield _sse({"type": "reasoning", "text": text})


async def _run_direct_answer(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """direct_qa / draft（问答场景）：检索 + 直答。

    无有用信息时按意图规格分流（用户 2026-10-13 硬性要求，V1.1 方案）：
    - 确切回答型（allows_self_capability=False，如 direct_qa）→ 固定话术
      委婉说明没有相关内容，不调用模型，从机制上杜绝编造；
    - 任务型（allows_self_capability=True，如 draft 公文起草/创作）→ 不拒答，
      改用模型自身能力完成任务（提示词切换 no_materials 模式），答案末尾
      追加甄别声明（disclaimer=True）。
    """
    retrieve_resp = await retrieve_enhanced(ctx.db, ctx.req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations
    ctx.citations[:] = citations
    # "无有用信息"双信号判定（向量距离 + 关键词命中，见 policies.is_no_useful_info）；
    # 阈值来自配置中心 retrieval.vec_distance_threshold（缺项回退代码默认）
    ctx.retrieval_cfg = await load_retrieval_config(ctx.db)
    ctx.prompt_templates = await load_prompt_templates(ctx.db)
    ctx.no_result = is_no_useful_info(retrieve_resp, ctx.retrieval_cfg.vec_distance_threshold)

    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name
    yield _sse(_meta_event(ctx, gen.model_name, citations, no_result=ctx.no_result))

    if ctx.no_result:
        spec = get_spec(ctx.intent.intent)
        if spec is not None and spec.allows_self_capability:
            # 任务型意图（起草/创作/完成任务）：知识库无材料也用自身能力完成，
            # 不得声称引用知识库，末尾追加甄别声明（统一收尾处处理）。
            ctx.disclaimer = True
            messages = build_chat_messages(
                ctx.req.query, ctx.history, citations, no_materials=True,
                templates=ctx.prompt_templates,
            )
            async for evt in _stream_model(ctx, messages, gen):
                yield evt
            return
        # 确切回答型：硬性要求（用户 2026-10-13）——无有用检索结果 →
        # 固定话术委婉说明没有相关内容，不调用模型，从机制上杜绝编造。
        async for chunk in stream_text(NO_RESULT_ANSWER):
            ctx.parts.append(chunk)
            yield _sse({"type": "token", "text": chunk})
        return

    messages = build_chat_messages(
        ctx.req.query, ctx.history, citations, templates=ctx.prompt_templates
    )
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


async def _run_react(ctx: _ChatCtx) -> AsyncGenerator[str, None]:
    """deep_reason：ReAct TAO 循环 → 最终生成。

    与 ``_run_step_plan``（一次性拆步）的本质区别：步骤不是计划定死的，
    而是每轮 Thought → Action → Observation 动态决策——**模型自己决定**
    是否检索、用哪个工具、要不要打开文档看原文、何时收尾；过程以 SSE
    全程透出（react_round / thought / action / observation），TAO 全链文本
    随助手消息落库可回看。

    用户 2026-10-16 明确要求（红线）：
    - **不拒答**：循环结束零引用时**不拒答、不做强制兜底检索**，直接进入
      最终生成——最终生成 prompt 会切到"通用知识兜底"分支，模型基于自身
      知识完成任务并标注依据不足，答案末尾统一附「AI 生成，请人工鉴别」。
    - **多轮上下文**：会话历史注入决策上下文（引擎 history 参数）。
    """
    cfg = await load_react_config(ctx.db)
    # 档位解析：请求级 react_mode > 配置中心默认档。
    # 2026-10-19 起档位只影响**检索广度**，轮数/时长默认都不设上限
    # （cfg.max_rounds=0 / cfg.budget_ms=0），由模型自主决定何时收尾。
    cfg = cfg.for_mode(getattr(ctx.req, "react_mode", None))
    result = ReactRunResult()

    # TAO 主循环：SSE 事件原样透传，结论（引用/轮数/trace）写入 result
    async for evt in run_react(
        ctx.db, ctx.req, cfg, result,
        caller=ctx.caller, history=ctx.history,
    ):
        yield _sse(evt)

    gen = ModelProvider(role="gen")
    ctx.model_name = gen.model_name

    # 引用：全部来自模型自主调用的工具结果（模型没调用检索就是 0 条，
    # 这**不是错误也不是拒答理由**——最终生成会走通用知识兜底完成任务）
    ctx.citations[:] = result.citations

    # TAO 全链文本追加到思考链：统一收尾处随助手消息落库（reasoning 字段），
    # 历史消息打开即以"默认折叠、可展开"面板回看完整思维链。
    if result.trace_text:
        ctx.reasoning_parts.append(result.trace_text)

    # 所有深度推理答案统一附 AI 声明（材料充分与否都提示人工鉴别）
    ctx.disclaimer = True

    yield _sse(_meta_event(ctx, gen.model_name, ctx.citations, no_result=False))
    # 阶段播报：最终生成开始（模型思考+组织答案通常 3-10 秒，此前无提示）
    yield _sse({
        "type": "phase", "phase": "answer",
        "label": "正在综合全部材料组织答案…",
    })
    messages = build_react_final_messages(
        ctx.req.query, ctx.history, ctx.citations, result.rounds_log,
        # 提示词可配置：配置中心覆盖 deep_prompt_final 时整段替换（空=内置默认）
        system_override=cfg.prompt_final,
    )
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
    # 深度推理（ReAct）：编排层在 deep_on 时把 direct_qa/complex_task
    # 动态改写为该意图后进入（stream_chat 内），此处仅登记分发
    "deep_reason": _run_react,
    "chitchat": _run_chitchat,
    "out_of_scope": _run_refuse,
}


async def stream_chat(
    db: AsyncSession, req: ChatRequest, actor: str = "anonymous", caller: Caller | None = None
):
    """智能问答流式生成器（SSE 文本片段）。

    Args:
        db: 数据库会话。
        req: 问答请求（agent_mode 缺省时读配置中心开关）。
        actor: 操作者身份（上游网关下发，用于意图识别审计留痕）。
        caller: 调用方（深度推理 doc_lookup 工具判定空间可见性用；可缺省）。
    """
    # 1) 会话收敛 + 空间范围确定（请求级 > 会话绑定 > 不限）
    conv = await _ensure_conversation(db, req)
    conversation_id = conv.id
    space_ids = _resolve_space_ids(req, conv.space_ids or [])
    # 把收敛后的空间范围写回请求对象：retrieve_enhanced 从 req.space_ids 读取，
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

    # 3.5) 首个阶段播报（2026-10-16 新增）：意图分类要 1-3 秒，此前从发出
    # 请求到第一个 SSE 事件之间**完全静默**，浏览器 fetch 一直挂着，用户看到
    # "点了没反应"。这里第一时间回一个轻量事件，让连接立刻建立、页面立刻动。
    yield _sse({"type": "phase", "phase": "start", "label": "正在准备…"})

    # 4) Agent 配置与意图识别（agent_mode 关闭时行为与旧版完全一致）
    agent_cfg = await load_agent_config(db)
    agent_on = req.agent_mode if req.agent_mode is not None else agent_cfg.enabled
    # 深度推理（ReAct）开关：请求级 deep_reason > 配置中心（默认全开，
    # 用户 2026-10-16 确认）。**仅接管问答类意图**（direct_qa / complex_task）
    # ——闲聊/拒答/起草意图不受影响；显式 deep_reason=false 即完全回到旧行为。
    react_cfg = await load_react_config(db)
    deep_on = req.deep_reason if req.deep_reason is not None else react_cfg.enabled
    if agent_on:
        yield _sse({"type": "phase", "phase": "classify", "label": "正在识别问题类型…"})
        intent_result = await IntentRouter(agent_cfg).classify(db, req.query)
        if deep_on and intent_result.intent in ("direct_qa", "complex_task"):
            intent_result = IntentResult(
                "deep_reason",
                intent_result.confidence,
                intent_result.channel,
                f"深度推理接管（原意图 {intent_result.intent}）",
            )
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
        # Agent 关闭：深度推理一并退出（纯直答，与旧版完全一致）
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
        caller=caller,
    )
    strategy = _CHAT_STRATEGIES.get(intent_result.intent, _run_direct_answer)

    async def _save_assistant(answer: str, hits: list[str], model: str):
        """落库助手消息（幂等由 ctx.saved 保证；答案为空则不落库）。

        intent / reasoning 随消息留痕：每轮意图独立保存（不被后续轮次
        覆盖），思考链全文可折叠展开查看。
        """
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
            intent=ctx.intent.intent,
            reasoning=ctx.reasoning_text or None,
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
