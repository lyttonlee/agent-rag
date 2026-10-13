"""推理生成：检索增强 + 强制引用溯源 + 敏感词后处理 + 流式输出。

设计铁律：
- 每处事实性内容必须带引用角标；无依据时必须明确提示，禁止静默编造。
- 生成后再做敏感词掩码，并将命中回报第三方（sensitive_hits）。
- 流式输出：先回传检索元数据（citations），再逐 token 流式返回正文，
  避免大草稿在等待期间被前端判定为"超时/假死"。
- 会话持久化（conversation_id）：公文起草同样支持多轮修改——
  user/assistant 消息落库，停止时保存部分答案。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.db.models import ConfigKV, Conversation
from app.schemas.contracts import Citation, GenerateRequest
from app.services import conversation as conv_service
from app.services.generation_prompt import build_generation_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import hybrid_retrieve

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


async def _ensure_draft_conversation(db: AsyncSession, req: GenerateRequest) -> str | None:
    """确保起草会话存在（缺省创建）；返回会话 ID（无会话诉求时 None）。"""
    if not req.conversation_id:
        return None
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == req.conversation_id))
    ).scalar_one_or_none()
    if conv:
        return conv.id
    logger.warning("起草会话 %s 不存在，自动创建", req.conversation_id)
    row = await conv_service.create_conversation(db, conv_type="draft")
    return row.id


async def generate_answer(db: AsyncSession, req: GenerateRequest) -> dict:
    """非流式生成（保留原有接口）。返回结构化结果字典。"""
    retrieve_resp = await hybrid_retrieve(db, req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations

    gen = ModelProvider(role="gen")
    messages = build_generation_messages(req, citations)
    raw = await gen.generate(messages)

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


async def stream_generate(db: AsyncSession, req: GenerateRequest):
    """流式生成，产出 SSE 文本片段（字符串）。

    事件序列：
      {"type":"meta","model":...,"conversation_id":...,"citations":[...]}  检索完成，先给上下文
      {"type":"token","text":"..."}                         正文逐片
      {"type":"reasoning","text":"..."}                     思考链（可选）
      {"type":"done","answer":...,"sensitive_hits":[...]}    收尾（含敏感词脱敏）
      {"type":"error","message":"..."}                      异常

    会话持久化：req.conversation_id 存在时，user/assistant 消息落库
    （公文起草多轮修改）；用户点「停止」时已生成部分自动保存。
    """
    # 会话收敛：无 conversation_id 时不建会话（保持旧行为兼容）
    conversation_id = await _ensure_draft_conversation(db, req)
    if conversation_id:
        await conv_service.append_message(db, conversation_id, "user", req.query)

    parts: list[str] = []
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
        )
        saved = True

    try:
        # 1) 检索上下文（含 rerank 优雅降级）
        retrieve_resp = await hybrid_retrieve(db, req)  # type: ignore[arg-type]
        citations: list[Citation] = retrieve_resp.citations
        citations_holder[:] = citations
        gen = ModelProvider(role="gen")
        messages = build_generation_messages(req, citations)

        # 2) 先回传检索元数据，前端可立即渲染"参考材料"
        yield _sse({
            "type": "meta",
            "model": gen.model_name,
            "conversation_id": conversation_id,
            "truncated_by_security": retrieve_resp.truncated_by_security,
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
        async for kind, text in gen.generate_stream(messages):
            if kind == "content":
                parts.append(text)
                yield _sse({"type": "token", "text": text})
            else:
                yield _sse({"type": "reasoning", "text": text})

        # 4) 收尾：敏感词脱敏后回传完整答案 + 落库助手消息
        raw = "".join(parts)
        sensitive_words = await _resolve_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        await _save_assistant(answer, citations, hits, gen.model_name)
        yield _sse({
            "type": "done",
            "answer": answer,
            "sensitive_hits": hits,
            "model": gen.model_name,
            "conversation_id": conversation_id,
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
