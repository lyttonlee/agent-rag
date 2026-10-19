"""推理生成：检索增强 + 强制引用溯源 + 敏感词后处理 + 流式输出。

设计铁律：
- 每处事实性内容必须带引用角标；无依据时必须明确提示，禁止静默编造。
- 生成后再做敏感词掩码，并将命中回报第三方（sensitive_hits）。
- 流式输出：先回传检索元数据（citations），再逐 token 流式返回正文，
  避免大草稿在等待期间被前端判定为"超时/假死"。
- 会话持久化（conversation_id）：公文起草同样支持多轮修改——
  user/assistant 消息落库，停止时保存部分答案；
- **多轮上下文注入（用户 2026-10-16 硬性要求）**：同一会话内再次提
  修改意见时，往轮 user 要求 + 上稿 assistant 草稿注入模型上下文
  （见 _load_draft_history / generation_prompt._DRAFT_REVISION_RULE），
  模型在上一稿基础上修改并输出完整文稿，而非重新起草。

**深度推理起草（用户 2026-10-19 要求：智能起草也按深度推理问答的方式、
由模型自主决策）**：
- 开启后（缺省跟随配置中心 ``deep_draft_enabled``，默认开；请求级
  ``deep_reason`` 可显式覆盖），起草不再是"拿 query 一把梭检索 + 直接
  成稿"，而是先由智能体跑 TAO 循环（Thought → Action → Observation）：
  **是否检索、查什么关键词、要不要打开原文核对、何时收尾，全部由模型
  逐轮自主决定**，材料够了再 finish 交由系统成稿；
- 与问答的 deep_reason 复用同一套引擎（``run_react``），仅提示词包换成
  起草通道（``DRAFT_PACK``）：智能体身份是"起草依据收集者"，finish 的
  语义是"依据已足够，开始起草"；
- 过程 SSE 全程透出（react_round / thought / action / observation +
  phase 阶段播报），TAO 全链文本随助手消息 reasoning 落库可回看；
- 关闭（``deep_reason=false``）时完全回退单趟旧链路（行为不变），
  可作为降级开关。

Agent 接入（P1，用户 2026-10-13 确认起草链路纳入）：
- 意图识别：template_doc_type 显式指定 → draft（强意图信号）；
  否则走 IntentRouter 分类，结果进入 meta 事件与审计（意图识别）；
- 无材料兜底：未检索到参考材料时，任务不失败——改用模型通用写作
  能力起草（提示词切换，不得虚构知识库依据），答案末尾追加
  TASK_DISCLAIMER 甄别声明，done 事件带 disclaimer 标志。
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.db.models import ConfigKV, Conversation, Template
from app.schemas.contracts import Citation, GenerateRequest
from app.services import conversation as conv_service
from app.services.agent import (
    DRAFT_DISCLAIMER,
    TASK_DISCLAIMER,
    IntentRouter,
    intent_label,
    is_no_useful_info,
    load_agent_config,
)
from app.services.agent.react_config import load_react_config
from app.services.agent.react_engine import ReactRunResult, run_react
from app.services.agent.react_prompt import DRAFT_PACK, format_process_summary
from app.services.audit import record_audit
from app.services.auth import Caller
from app.services.generation_prompt import build_generation_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import retrieve_enhanced
from app.services.runtime_config import load_prompt_templates, load_retrieval_config

logger = logging.getLogger(__name__)

# 多轮上下文：取最近 N 条消息（约 N/2 轮"要求→草稿"）。
# 与 chat 链路 _HISTORY_LIMIT 对齐；草稿较长，截断上限在
# generation_prompt 中单独放宽（assistant 6000 / user 1000 字符）。
_DRAFT_HISTORY_LIMIT = 10

# 深度推理起草：成稿阶段注入模型的单条引用材料字符上限。
# read_document 返回的原文章节可达 4000 字，直接全量注入会让成稿 prompt
# 臃塞（Qwen3 上下文珍贵）；起草只需要关键条款，超长部分模型可再检索。
_DRAFT_CITATION_MAX_CHARS = 1500
# 成稿生成 max_tokens：公文 2000-3000 字 + Qwen3 思考链与正文**共用**预算
# （2026-10-16 踩坑：默认 4096 时长文会被截断），起草场景再放宽一档。
_DRAFT_GEN_MAX_TOKENS = 8192


async def _resolve_sensitive_words(db: AsyncSession, req: GenerateRequest) -> list[str]:
    """解析本次生成使用的敏感词表。

    优先级：请求级覆盖（req.sensitive_words）> 配置中心词库
    （ConfigKV category=sensitive_word 且 is_active）。词库由运维台
    「配置中心 → 敏感词」页面维护，用户自行输入词条，保存即生效。
    """
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


async def _ensure_draft_conversation(
    db: AsyncSession, req: GenerateRequest, doc_type: str | None = None
) -> str | None:
    """确保起草会话存在（缺省自动创建）；返回会话 ID。

    与 chat 链路 _ensure_conversation 对齐（用户 2026-10-15 需求：智能起草
    历史会话缺失的根因修复）：此前 conversation_id 为空时直接返回 None，
    导致 stream_generate 的 meta/done 事件 conversation_id 恒为 null，
    前端 onMeta 永远不落会话、历史侧栏看不到起草记录、多轮修改无法延续。

    修复策略：
    - conversation_id 缺省（首次起草）→ 自动创建 draft 会话；
    - 指定 ID 但会话不存在（前端误传）→ 静默新建，保证起草不中断；
    - 指定 ID 且存在 → 沿用（多轮修改落到同一会话）。

    绑定 doc_type（文种，供历史还原回显）与 space_ids（检索范围），
    与 chat 会话的空间绑定语义保持一致。
    """
    if req.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.id == req.conversation_id)
            )
        ).scalar_one_or_none()
        if conv:
            return conv.id
        # 前端误传了不存在的会话 ID：静默新建，保证起草不中断（与 chat 一致）
        logger.warning("起草会话 %s 不存在，自动创建新会话", req.conversation_id)
    # 缺省（首次起草）或指定会话不存在 → 自动创建 draft 会话并落库文种/检索范围
    row = await conv_service.create_conversation(
        db,
        conv_type="draft",
        doc_type=doc_type,
        space_ids=req.space_ids or [],
    )
    return row.id


async def _load_draft_history(
    db: AsyncSession, conversation_id: str | None, current_query: str
) -> list[tuple[str, str]]:
    """加载起草会话的多轮历史（排除刚写入的本轮 user 消息）。

    多轮修改的关键（用户 2026-10-16 硬性要求，同会话一致 conversation_id）：
    同一会话内再次提修改意见时，必须把往轮 user 要求 + 上稿 assistant
    草稿注入模型上下文。此前两条生成链路都只把本轮 query + 检索材料交给
    模型，导致"改个标题"也重新生成一份完整文稿——上一稿完全丢失。

    Returns:
        [(role, content), ...] 按时间正序；无会话 / 首轮为空列表。
    """
    if not conversation_id:
        return []
    rows = await conv_service.recent_history(db, conversation_id, limit=_DRAFT_HISTORY_LIMIT)
    return [
        (m.role, m.content)
        for m in rows
        if not (m.role == "user" and m.content == current_query)
    ]


async def _resolve_template(
    db: AsyncSession, req: GenerateRequest, caller: Caller | None = None
) -> tuple[str | None, str | None]:
    """解析起草模板：优先 template_code 精确定位，否则按 doc_type 取首个启用模板。

    返回 (doc_type, name)；均未命中时回退 (req.template_doc_type, None)。
    同文种可配置多模板（如多类「通知」），用户选定具体模板后 code 精确命中，
    使文种指令带出"模板：XXX"，生成更贴合所选模板。

    所有权（用户 2026-10-14 需求）：caller 非空时仅能解析
    「系统标准模板 + 本人模板」；显式指定的 template_code 属于他人时
    直接 403（不静默回退——避免"选了模板却没生效"的困惑，也不暴露
    模板内容，与列表过滤的"不可见"语义一致）。
    """
    if req.template_code:
        row = (
            await db.execute(
                select(Template).where(Template.code == req.template_code)
            )
        ).scalar_one_or_none()
        if row and caller is not None and not (
            row.owner_type == "system"
            or (
                row.owner_type == ("user" if caller.kind == "user" else "client")
                and row.owner_id == caller.id
            )
        ):
            raise HTTPException(
                status_code=403, detail="无权使用该模板（仅创建者与系统标准模板可用）"
            )
        if row and row.is_active:
            return row.doc_type, row.name
    if req.template_doc_type:
        stmt = select(Template).where(
            Template.doc_type == req.template_doc_type, Template.is_active.is_(True)
        )
        if caller is not None:
            kind = "user" if caller.kind == "user" else "client"
            stmt = stmt.where(
                or_(
                    Template.owner_type == "system",
                    (Template.owner_type == kind) & (Template.owner_id == caller.id),
                )
            )
        row = (await db.execute(stmt.order_by(Template.created_at))).scalars().first()
        if row:
            return row.doc_type, row.name
    return req.template_doc_type, None


def _build_draft_task_context(
    req: GenerateRequest, template_name: str | None, has_history: bool
) -> str:
    """起草通道的任务上下文（注入 ReAct 首条决策提示）。

    让智能体第 1 轮就知道三件事：这是**公文起草**任务（不是问答）、
    目标文种是什么、finish 之后会发生什么（系统据全部材料成稿）。
    没有这段上下文，起草通道的模型容易按"回答问题"的方式收尾，
    产出大纲而不是完整公文的依据集合。
    """
    doc_type = (req.template_doc_type or "").strip()
    lines = ["【任务类型】公文起草（不是问答）。"]
    lines.append(f"【目标文种】{doc_type or '未指定，请根据主题与用途判断'}")
    if template_name:
        lines.append(f"【采用模板】{template_name}")
    lines.append(
        "【你的任务】收集足够的起草依据（相关规定、口径、同类文件写法）；"
        "finish 之后由系统依据全部检索材料与文种模板生成完整公文草稿。"
    )
    if has_history:
        lines.append(
            "【注意】会话历史中可能已有上一稿：本轮是在上一稿基础上修改，"
            "请把修改所需的新依据查全（文种与结构通常保持不变）。"
        )
    return "\n".join(lines)


def _cap_citations(citations: list[Citation], max_chars: int) -> list[Citation]:
    """按字符上限裁剪引用材料（起草成稿 prompt 专用）。

    read_document 的原文章节可达 4000 字；成稿只需要关键条款，超长材料
    全量注入会挤占上下文。裁剪的是**注入模型的副本**，落库与前端展示
    用的 citations 保持完整（截断会丢溯源信息）。
    """
    capped: list[Citation] = []
    for c in citations:
        snippet = (c.snippet or "")[:max_chars]
        content = (c.content or "")[:max_chars] if c.content else None
        capped.append(c.model_copy(update={"snippet": snippet, "content": content}))
    return capped


async def assert_template_usable(
    db: AsyncSession, req: GenerateRequest, caller: Caller | None
) -> None:
    """路由入口的模板所有权校验（用户 2026-10-14 需求）。

    在进入生成链路（尤其 SSE 流式，响应头已发出无法再改状态码）
    之前拦截：显式指定的 template_code 若属于他人 → 403。
    与 _resolve_template 内部过滤互为双保险（防御性编程）。
    """
    if caller is None or not req.template_code:
        return
    row = (
        await db.execute(select(Template).where(Template.code == req.template_code))
    ).scalar_one_or_none()
    if row and not (
        row.owner_type == "system"
        or (
            row.owner_type == ("user" if caller.kind == "user" else "client")
            and row.owner_id == caller.id
        )
    ):
        raise HTTPException(
            status_code=403, detail="无权使用该模板（仅创建者与系统标准模板可用）"
        )


async def _stream_draft_react(
    db: AsyncSession,
    req: GenerateRequest,
    actor: str,
    caller: Caller | None,
    *,
    conversation_id: str | None,
    history: list[tuple[str, str]],
    template_name: str | None,
    intent_result=None,
) -> AsyncGenerator[str, None]:
    """深度推理起草（ReAct）：智能体自主收集依据 → 依据全部材料成稿。

    事件序列（比单趟链路多出整段 TAO 过程，前端据此渲染推理面板）：
      plan（4 步：理解需求/收集依据/成稿/脱敏）
      → step_start(understand)
      → react_start / analysis / phase / react_round / thought / action /
        observation … / react_end   ← 全部原样透传（含 step_start(collect)）
      → step_end(collect) → meta → step_start(generate) → phase(answer)
      → token* / reasoning* → step_end(generate)
      → step_start(redact) → step_end(redact) → done

    与单趟链路的本质区别：**检索什么、要不要看原文、何时收尾由模型逐轮
    自主决定**，编排层不预设任何检索动作；finish 之后才成稿。

    产品红线（与问答 deep_reason 一致，用户 2026-10-16 要求）：
    - **不拒答**：循环零引用也照常成稿（提示词切到"通用公文规范"分支，
      依据不足处由模型标注），不得只回一句"无法起草"；
    - **多轮上下文**：会话历史（含上一稿）注入决策上下文，模型知道
      "这一轮是在改上一稿"；
    - **过程可见**：TAO 全链文本随助手消息 reasoning 落库，历史可回看。
    """
    parts: list[str] = []
    reasoning_parts: list[str] = []
    citations_holder: list[Citation] = []
    saved = False

    async def _save_assistant(answer: str, citations: list, hits: list[str], model: str):
        """落库助手消息（幂等由 saved 保证；无会话诉求时直接跳过）。"""
        nonlocal saved
        if not conversation_id or saved or not answer.strip():
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
                for c in citations
            ],
            sensitive_hits=hits,
            model=model,
            intent=intent_result.intent if intent_result else None,
            reasoning="".join(reasoning_parts) or None,
        )
        saved = True

    # ---- 配置：档位解析（请求级 react_mode > 配置中心默认档）----
    cfg = await load_react_config(db)
    cfg = cfg.for_mode(getattr(req, "react_mode", None))
    result = ReactRunResult()
    task_ctx = _build_draft_task_context(req, template_name, bool(history))

    # ---- 步骤计划（过程可见）：理解需求 → 多轮收集依据 → 成稿 → 脱敏 ----
    yield _sse({
        "type": "plan",
        "steps": [
            {"id": "understand", "action": "understand", "query": req.query, "status": "pending"},
            {"id": "collect", "action": "collect", "query": req.query, "status": "pending"},
            {"id": "generate", "action": "generate", "query": req.query, "status": "pending"},
            {"id": "redact", "action": "redact", "query": req.query, "status": "pending"},
        ],
        "channel": "draft",
        "mode": "react",
    })
    yield _sse({"type": "step_start", "id": "understand", "action": "understand", "query": req.query})

    collect_open = False
    try:
        # ---- TAO 主循环：SSE 事件透传，步骤时间线同步收尾 ----
        async for evt in run_react(
            db, req, cfg, result,
            caller=caller, history=history,
            pack=DRAFT_PACK, task_context=task_ctx,
        ):
            etype = evt.get("type")
            if etype == "analysis":
                # 理解阶段结束（模型已复述起草诉求）→ 进入依据收集阶段
                yield _sse({"type": "step_end", "id": "understand", "ok": True,
                            "summary": "已理解起草需求"})
                yield _sse(evt)
                yield _sse({"type": "step_start", "id": "collect", "action": "collect",
                            "query": req.query})
                collect_open = True
            elif etype == "react_end":
                yield _sse(evt)
                if collect_open:
                    yield _sse({
                        "type": "step_end", "id": "collect", "ok": True,
                        "summary": (
                            f"共 {evt.get('rounds', 0)} 轮，"
                            f"收集起草依据 {evt.get('citations_total', 0)} 条"
                        ),
                    })
                    collect_open = False
            else:
                yield _sse(evt)
        # 防御：极端情况下（理解阶段异常中断）补收 collect 步骤
        if collect_open:
            yield _sse({"type": "step_end", "id": "collect", "ok": True,
                        "summary": "依据收集结束"})

        # TAO 全链文本随消息落库（回答结束后可在折叠面板回看）
        if result.trace_text:
            reasoning_parts.append(result.trace_text)

        gen = ModelProvider(role="gen")
        citations = result.citations
        citations_holder[:] = citations
        # 无材料 → 通用公文规范成稿（不拒答）；有材料 → 强制引用标注
        no_materials = not citations

        yield _sse({
            "type": "meta",
            "model": gen.model_name,
            "conversation_id": conversation_id,
            "truncated_by_security": False,
            "agent_mode": True,
            "intent": intent_result.intent if intent_result else None,
            "intent_label": intent_label(intent_result.intent) if intent_result else None,
            "no_result": False,
            "deep_reason": True,
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
        })

        # ---- 成稿：推理过程 + 裁剪后的引用材料 + 用户要求 → 流式生成 ----
        templates = await load_prompt_templates(db)
        messages = build_generation_messages(
            req,
            _cap_citations(citations, _DRAFT_CITATION_MAX_CHARS),
            no_materials=no_materials,
            templates=templates,
            template_name=template_name,
            history=history,
            process=format_process_summary(result.rounds_log),
        )
        yield _sse({"type": "step_start", "id": "generate", "action": "generate",
                    "query": req.query})
        yield _sse({"type": "phase", "phase": "answer",
                    "label": "正在依据全部材料起草原文…"})
        async for kind, text in gen.generate_stream(messages, max_tokens=_DRAFT_GEN_MAX_TOKENS):
            if kind == "content":
                parts.append(text)
                yield _sse({"type": "token", "text": text})
            else:
                reasoning_parts.append(text)
                yield _sse({"type": "reasoning", "text": text})
        yield _sse({
            "type": "step_end", "id": "generate", "ok": True,
            "summary": f"已生成草稿（约 {len(''.join(parts))} 字）",
        })

        # ---- 收尾：AI 声明 + 敏感词脱敏 + 落库 + done ----
        raw = "".join(parts)
        if raw.strip():
            raw += DRAFT_DISCLAIMER
            yield _sse({"type": "token", "text": DRAFT_DISCLAIMER})
        yield _sse({"type": "step_start", "id": "redact", "action": "redact",
                    "query": req.query})
        sensitive_words = await _resolve_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        yield _sse({
            "type": "step_end", "id": "redact", "ok": True,
            "summary": (
                f"命中 {len(hits)} 处敏感词，已脱敏" if hits else "未发现敏感词"
            ),
        })
        await _save_assistant(answer, citations, hits, gen.model_name)
        yield _sse({
            "type": "done",
            "answer": answer,
            "sensitive_hits": hits,
            "model": gen.model_name,
            "conversation_id": conversation_id,
            "intent": intent_result.intent if intent_result else None,
            "no_result": False,
            "disclaimer": True,
        })
    except GeneratorExit:
        # 用户点「停止」：保存已生成部分（未脱敏，留痕可继续）
        logger.info("深度推理起草被中止，保存部分答案（conversation=%s）", conversation_id)
        await _save_assistant("".join(parts), citations_holder, [], "unknown")
        raise
    except Exception as exc:  # 不崩溃连接，向前端推送错误事件
        logger.exception("深度推理起草异常：%s", exc)
        try:
            await _save_assistant("".join(parts), citations_holder, [], "unknown")
        except Exception:  # pragma: no cover - 兜底不影响主错误上报
            pass
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})


async def generate_answer(
    db: AsyncSession, req: GenerateRequest, caller: Caller | None = None
) -> dict:
    """非流式生成（保留原有接口）。返回结构化结果字典。

    深度推理起草（deep_reason，2026-10-19）：开启时先跑 ReAct TAO 循环
    （智能体自主收集依据），再一次性成稿——与流式版同一策略，只是丢弃
    SSE 事件、不逐 token 输出。关闭时走单趟旧链路（行为不变）。

    两链路共用的无材料兜底：未检索到参考材料时任务不失败——切换自身
    能力起草（提示词切换，不得虚构知识库依据），答案追加甄别声明。

    caller：当前认证主体（用户/第三方），用于模板所有权解析——
    仅能使用系统标准模板与本人模板（用户 2026-10-14 需求）；
    脚本等无认证上下文场景传 None（不过滤，向后兼容）。
    """
    # 解析起草模板（code 优先 / doc_type 兜底），把文种与模板名注入提示词
    resolved_doc_type, template_name = await _resolve_template(db, req, caller)
    req.template_doc_type = resolved_doc_type

    # 深度推理开关：请求级 deep_reason > 配置中心 deep_draft_enabled（默认开）
    react_cfg = await load_react_config(db)
    deep_on = req.deep_reason if req.deep_reason is not None else react_cfg.draft_enabled
    if deep_on:
        return await _generate_answer_react(db, req, caller, react_cfg, template_name)

    retrieve_resp = await retrieve_enhanced(db, req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations
    # 检索参数（向量距离阈值）与提示词模板均来自配置中心，缺省回退默认
    cfg = await load_retrieval_config(db)
    templates = await load_prompt_templates(db)
    no_materials = is_no_useful_info(retrieve_resp, cfg.vec_distance_threshold)

    gen = ModelProvider(role="gen")
    # 多轮上下文：指定了会话 ID 时注入往轮要求与上稿（在上一稿基础上修改）
    history = await _load_draft_history(db, req.conversation_id, req.query)
    messages = build_generation_messages(
        req, citations, no_materials=no_materials, templates=templates,
        template_name=template_name, history=history,
    )
    raw = await gen.generate(messages)
    if no_materials:
        raw += TASK_DISCLAIMER

    sensitive_words = await _resolve_sensitive_words(db, req)
    answer, hits = redact_sensitive(raw, sensitive_words)
    return {
        "answer": answer,
        "citations": citations,
        "sensitive_hits": hits,
        "model": gen.model_name,
        "truncated_by_security": retrieve_resp.truncated_by_security,
    }


async def _generate_answer_react(
    db: AsyncSession,
    req: GenerateRequest,
    caller: Caller | None,
    react_cfg,
    template_name: str | None,
) -> dict:
    """非流式深度推理起草：跑完 TAO 循环后一次性成稿。

    循环内事件全部丢弃（非流式没有消费方）；结论（引用/轮数/过程摘要）
    照常写入 result 供成稿使用。
    """
    cfg = react_cfg.for_mode(getattr(req, "react_mode", None))
    result = ReactRunResult()
    history = await _load_draft_history(db, req.conversation_id, req.query)
    task_ctx = _build_draft_task_context(req, template_name, bool(history))
    async for _ in run_react(
        db, req, cfg, result,
        caller=caller, history=history,
        pack=DRAFT_PACK, task_context=task_ctx,
    ):
        pass  # 非流式：不消费 SSE 事件

    citations = result.citations
    gen = ModelProvider(role="gen")
    templates = await load_prompt_templates(db)
    messages = build_generation_messages(
        req,
        _cap_citations(citations, _DRAFT_CITATION_MAX_CHARS),
        no_materials=not citations,
        templates=templates,
        template_name=template_name,
        history=history,
        process=format_process_summary(result.rounds_log),
    )
    raw = await gen.generate(messages, max_tokens=_DRAFT_GEN_MAX_TOKENS)
    raw += DRAFT_DISCLAIMER

    sensitive_words = await _resolve_sensitive_words(db, req)
    answer, hits = redact_sensitive(raw, sensitive_words)
    return {
        "answer": answer,
        "citations": citations,
        "sensitive_hits": hits,
        "model": gen.model_name,
        "truncated_by_security": False,
    }


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream_generate(
    db: AsyncSession, req: GenerateRequest, actor: str = "anonymous",
    caller: Caller | None = None,
):
    """流式生成，产出 SSE 文本片段（字符串）。

    **深度推理起草**（deep_reason，缺省跟随配置中心 deep_draft_enabled，
    默认开）事件序列：
      plan（4 步：理解需求/收集依据/成稿/脱敏，channel="draft",mode="react"）
      → react_start / analysis / phase / react_round / thought / action /
        observation … / react_end   ← 智能体自主决策全过程（可渲染推理面板）
      → meta → token*（正文逐片）/ reasoning*（思考链）→ done

    **单趟链路**（deep_reason=false 或配置关闭）事件序列：
      {"type":"plan","steps":[...],"channel":"draft"}   步骤计划（过程可见）
      {"type":"step_start","id":...,"action":...}       某步骤开始（检索/生成/脱敏）
      {"type":"meta","model":...,"conversation_id":...,"intent":...,
       "citations":[...],"no_result":...}              检索完成，先给上下文
      {"type":"token","text":"..."}                     正文逐片
      {"type":"reasoning","text":"..."}                 思考链（可选）
      {"type":"step_end","id":...,"ok":...,"summary":...}  某步骤结束（含结论摘要）
      {"type":"done","answer":...,"sensitive_hits":[...],
       "disclaimer":...}                               收尾（含敏感词脱敏）
      {"type":"error","message":"..."}                  异常

    未知事件类型（如老客户端收到新事件）必须忽略——SSE 协议向后兼容约定。

    步骤时间线（用户 2026-10-15 需求 #5：页面需看到"当前到哪一步"）：
    单趟起草不做 complex_task 拆步，但把关键阶段（检索参考材料 → 起草生成
    → 敏感词脱敏）作为步骤透出，与 chat 链路的 plan/step 事件对齐，前端
    AgentStepPanel 据此渲染实时推进的时间线；深度推理链路换成
    「理解需求 → 多轮收集依据 → 成稿 → 脱敏」四步（TAO 细节走 react_* 事件）。

    会话持久化：req.conversation_id 缺省时自动创建 draft 会话（见
    _ensure_draft_conversation），user/assistant 消息落库（公文起草多轮修改）；
    用户点「停止」时已生成部分自动保存。

    Agent（P1）：意图识别结果进 meta/审计；未检索到材料时切换自身能力
    起草并追加甄别声明（disclaimer）。
    """
    # 解析起草模板（code 优先 / doc_type 兜底）：拿到文种与模板名，供文种指令
    # 与（draft 会话）文种落库回显使用。需在建会话前完成，以便把 doc_type 写入会话。
    # caller 传入以执行模板所有权过滤（他人模板 403）。
    resolved_doc_type, template_name = await _resolve_template(db, req, caller)
    req.template_doc_type = resolved_doc_type

    # 会话收敛：无 conversation_id 时不建会话（保持旧行为兼容）
    conversation_id = await _ensure_draft_conversation(db, req, doc_type=resolved_doc_type)
    if conversation_id:
        await conv_service.append_message(db, conversation_id, "user", req.query)

    # 多轮上下文（用户 2026-10-16 硬性要求）：同一会话内再次提修改意见时，
    # 注入往轮 user 要求 + 上稿 assistant 草稿——否则模型只看到本轮 query
    # 与检索材料，会重新生成一份完整文稿而不是在上一稿基础上修改。
    # 注意顺序：必须先 append 本轮 user 消息（上面），再把"除本轮外"的
    # 历史取出来，保证首轮为空、修改轮带上上稿。
    history = await _load_draft_history(db, conversation_id, req.query)

    # Agent 意图识别：显式文种 → draft（强信号）；否则规则+LLM 分类。
    # agent_mode 关闭时跳过分类，meta 不带 intent，行为与旧版一致。
    agent_cfg = await load_agent_config(db)
    agent_on = req.agent_mode if req.agent_mode is not None else agent_cfg.enabled
    if agent_on:
        intent_result = await IntentRouter(agent_cfg).classify(
            db, req.query, template_doc_type=req.template_doc_type
        )
        await record_audit(
            actor=actor,
            actor_ip=None,
            method="POST",
            endpoint="/v1/generate/stream",
            status_code=200,
            object_type="conversation",
            object_id=conversation_id or "",
            action="意图识别",
            result="success",
            detail=(
                f"intent={intent_result.intent} channel={intent_result.channel} "
                f"confidence={intent_result.confidence:.2f} "
                f"reason={intent_result.reason} query={req.query[:80]}"
            ),
        )
    else:
        intent_result = None

    # ---- 深度推理起草分流（用户 2026-10-19：智能起草改为模型自主决策）----
    # 开关优先级：请求级 deep_reason > 配置中心 deep_draft_enabled（默认开）。
    # 开启 → ReAct TAO 循环自主收集依据后成稿（_stream_draft_react）；
    # 关闭 → 下方单趟旧链路（拿 query 一把梭检索 + 直接成稿，行为不变）。
    react_cfg = await load_react_config(db)
    draft_deep = req.deep_reason if req.deep_reason is not None else react_cfg.draft_enabled
    if draft_deep:
        async for chunk in _stream_draft_react(
            db, req, actor, caller,
            conversation_id=conversation_id,
            history=history,
            template_name=template_name,
            intent_result=intent_result,
        ):
            yield chunk
        return

    parts: list[str] = []
    reasoning_parts: list[str] = []   # 思考链片段（随消息落库，回答后可展开查看）
    citations_holder: list[Citation] = []
    saved = False
    disclaimer = False

    async def _save_assistant(answer: str, citations: list, hits: list[str], model: str):
        """落库助手消息（幂等由 saved 保证；无会话诉求时直接跳过）。

        intent / reasoning 随消息留痕：起草链路的每轮意图与思考链
        同样独立保存，历史会话可回看（回答结束后不丢失）。
        """
        nonlocal saved
        if not conversation_id or saved or not answer.strip():
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
                for c in citations
            ],
            sensitive_hits=hits,
            model=model,
            intent=intent_result.intent if intent_result else None,
            reasoning="".join(reasoning_parts) or None,
        )
        saved = True

    # 步骤计划（过程可见）：检索参考材料 → 起草生成 → 敏感词脱敏。
    # 单趟起草不做 complex_task 拆解，但把关键阶段作为步骤时间线透出，
    # 满足"页面能看到当前到哪一步"的需求（与 chat 链路 plan/step 事件对齐）。
    yield _sse({
        "type": "plan",
        "steps": [
            {"id": "retrieve", "action": "retrieve", "query": req.query, "status": "pending"},
            {"id": "generate", "action": "generate", "query": req.query, "status": "pending"},
            {"id": "redact", "action": "redact", "query": req.query, "status": "pending"},
        ],
        "channel": "draft",
    })

    try:
        # 1) 检索上下文（含 rerank 优雅降级）
        yield _sse({"type": "step_start", "id": "retrieve", "action": "retrieve", "query": req.query})
        retrieve_resp = await retrieve_enhanced(db, req)  # type: ignore[arg-type]
        citations: list[Citation] = retrieve_resp.citations
        citations_holder[:] = citations
        # 无有用信息（双信号判定）：任务不失败，切换自身能力起草 + 甄别声明
        # 阈值与提示词模板均来自配置中心（缺项回退默认）
        cfg = await load_retrieval_config(db)
        templates = await load_prompt_templates(db)
        no_materials = is_no_useful_info(retrieve_resp, cfg.vec_distance_threshold)
        disclaimer = no_materials  # 无材料兜底：答案需追加甄别声明
        yield _sse({
            "type": "step_end",
            "id": "retrieve",
            "ok": True,
            "summary": (
                "未检索到参考材料，将基于通用能力起草"
                if no_materials
                else f"检索到 {len(citations)} 条参考材料"
            ),
        })
        gen = ModelProvider(role="gen")
        messages = build_generation_messages(
            req, citations, no_materials=no_materials, templates=templates,
            template_name=template_name, history=history,
        )

        # 2) 先回传检索元数据，前端可立即渲染"参考材料"与意图标签
        yield _sse({
            "type": "meta",
            "model": gen.model_name,
            "conversation_id": conversation_id,
            "truncated_by_security": retrieve_resp.truncated_by_security,
            "agent_mode": agent_on,
            "intent": intent_result.intent if intent_result else None,
            "intent_label": intent_label(intent_result.intent) if intent_result else None,
            "no_result": no_materials,
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
        })

        # 3) 流式生成（思考链 + 正文）
        yield _sse({"type": "step_start", "id": "generate", "action": "generate", "query": req.query})
        async for kind, text in gen.generate_stream(messages):
            if kind == "content":
                parts.append(text)
                yield _sse({"type": "token", "text": text})
            else:
                reasoning_parts.append(text)
                yield _sse({"type": "reasoning", "text": text})

        # 生成阶段结束：回填步骤结论（字数），供前端时间线展示
        yield _sse({
            "type": "step_end",
            "id": "generate",
            "ok": True,
            "summary": f"已生成草稿（约 {len(''.join(parts))} 字）",
        })

        # 4) 收尾：敏感词脱敏后回传完整答案 + 落库助手消息
        raw = "".join(parts)
        if disclaimer and raw.strip():
            # 无材料兜底：追加"需自行甄别"声明（流式视图与最终答案一致）
            raw += TASK_DISCLAIMER
            yield _sse({"type": "token", "text": TASK_DISCLAIMER})
        yield _sse({"type": "step_start", "id": "redact", "action": "redact", "query": req.query})
        sensitive_words = await _resolve_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        yield _sse({
            "type": "step_end",
            "id": "redact",
            "ok": True,
            "summary": (
                f"命中 {len(hits)} 处敏感词，已脱敏" if hits else "未发现敏感词"
            ),
        })
        await _save_assistant(answer, citations, hits, gen.model_name)
        yield _sse({
            "type": "done",
            "answer": answer,
            "sensitive_hits": hits,
            "model": gen.model_name,
            "conversation_id": conversation_id,
            "intent": intent_result.intent if intent_result else None,
            "no_result": no_materials,
            "disclaimer": disclaimer,
        })
    except GeneratorExit:
        # 用户点「停止」：保存已生成部分（未脱敏，留痕可继续）
        logger.info("起草生成被中止，保存部分答案（conversation=%s）", conversation_id)
        await _save_assistant("".join(parts), citations_holder, [], "unknown")
        raise
    except Exception as exc:  # 不崩溃连接，向前端推送错误事件
        logger.exception("流式生成异常：%s", exc)
        try:
            await _save_assistant("".join(parts), citations_holder, [], "unknown")
        except Exception:  # pragma: no cover - 兜底不影响主错误上报
            pass
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
