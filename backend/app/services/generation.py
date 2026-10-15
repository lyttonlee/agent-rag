"""推理生成：检索增强 + 强制引用溯源 + 敏感词后处理 + 流式输出。

设计铁律：
- 每处事实性内容必须带引用角标；无依据时必须明确提示，禁止静默编造。
- 生成后再做敏感词掩码，并将命中回报第三方（sensitive_hits）。
- 流式输出：先回传检索元数据（citations），再逐 token 流式返回正文，
  避免大草稿在等待期间被前端判定为"超时/假死"。
- 会话持久化（conversation_id）：公文起草同样支持多轮修改——
  user/assistant 消息落库，停止时保存部分答案。

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
    TASK_DISCLAIMER,
    IntentRouter,
    intent_label,
    is_no_useful_info,
    load_agent_config,
)
from app.services.audit import record_audit
from app.services.auth import Caller
from app.services.generation_prompt import build_generation_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import retrieve_enhanced
from app.services.runtime_config import load_prompt_templates, load_retrieval_config

logger = logging.getLogger(__name__)


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


async def generate_answer(
    db: AsyncSession, req: GenerateRequest, caller: Caller | None = None
) -> dict:
    """非流式生成（保留原有接口）。返回结构化结果字典。

    与 stream_generate 保持一致的无材料兜底：未检索到参考材料时切换
    自身能力提示词，答案追加甄别声明。

    caller：当前认证主体（用户/第三方），用于模板所有权解析——
    仅能使用系统标准模板与本人模板（用户 2026-10-14 需求）；
    脚本等无认证上下文场景传 None（不过滤，向后兼容）。
    """
    retrieve_resp = await retrieve_enhanced(db, req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations
    # 检索参数（向量距离阈值）与提示词模板均来自配置中心，缺省回退默认
    cfg = await load_retrieval_config(db)
    templates = await load_prompt_templates(db)
    no_materials = is_no_useful_info(retrieve_resp, cfg.vec_distance_threshold)

    # 解析起草模板（code 优先 / doc_type 兜底），把文种与模板名注入提示词
    resolved_doc_type, template_name = await _resolve_template(db, req, caller)
    req.template_doc_type = resolved_doc_type

    gen = ModelProvider(role="gen")
    messages = build_generation_messages(
        req, citations, no_materials=no_materials, templates=templates,
        template_name=template_name,
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


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream_generate(
    db: AsyncSession, req: GenerateRequest, actor: str = "anonymous",
    caller: Caller | None = None,
):
    """流式生成，产出 SSE 文本片段（字符串）。

    事件序列：
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

    步骤时间线（用户 2026-10-15 需求 #5：页面需看到"当前到哪一步"）：
    单趟起草不做 complex_task 拆步，但把关键阶段（检索参考材料 → 起草生成
    → 敏感词脱敏）作为步骤透出，与 chat 链路的 plan/step 事件对齐，前端
    AgentStepPanel 据此渲染实时推进的时间线。

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
            template_name=template_name,
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
