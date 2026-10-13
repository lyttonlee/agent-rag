"""智能问答服务：多轮会话 + 检索增强 + 流式输出。

与公文起草（services/generation.py）的差异：
- 多轮：自动拼接会话历史（最近 N 轮）作为模型上下文，检索只用当前轮
  query（避免历史问题稀释召回精度，指代消解交给模型结合历史理解）；
- 会话持久化：user / assistant 消息落库，引用与敏感词命中随 assistant
  消息留痕，历史回看可核对依据；
- 用户点「停止」时 generator 被关闭（GeneratorExit），已生成部分自动
  保存为 assistant 消息，不丢答案。

事件序列（SSE）：
  {"type":"meta","conversation_id":...,"model":...,"citations":[...]}
  {"type":"token","text":"..."} / {"type":"reasoning","text":"..."}
  {"type":"done","answer":...,"sensitive_hits":[...],...}
  {"type":"error","message":"..."}
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.db.models import ConfigKV, Conversation
from app.schemas.contracts import ChatRequest
from app.services import conversation as conv_service
from app.services.generation_prompt import build_chat_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import hybrid_retrieve

logger = logging.getLogger(__name__)

# 多轮上下文：取最近 N 条消息（约 N/2 轮问答）
_HISTORY_LIMIT = 10


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


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


async def stream_chat(db: AsyncSession, req: ChatRequest):
    """智能问答流式生成器（SSE 文本片段）。"""
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

    # 局部状态：已生成正文 + 是否已落库（stop/完成只存一次，避免并发串话）
    parts: list[str] = []
    citations_holder: list = []
    saved = False

    async def _save_assistant(answer: str, citations: list, hits: list[str], model: str):
        """落库助手消息（幂等由 saved 保证）。"""
        nonlocal saved
        if saved or not answer.strip():
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
        # 4) 检索（空间 + 密级/权限下推；rerank 失败优雅回退 RRF）
        retrieve_resp = await hybrid_retrieve(db, req)  # type: ignore[arg-type]
        citations = retrieve_resp.citations
        citations_holder[:] = citations

        # 5) 先回传会话与检索元数据
        gen = ModelProvider(role="gen")
        yield _sse({
            "type": "meta",
            "conversation_id": conversation_id,
            "model": gen.model_name,
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

        # 6) 流式生成（思考链 + 正文）
        messages = build_chat_messages(req.query, history, citations)
        async for kind, text in gen.generate_stream(messages):
            if kind == "content":
                parts.append(text)
                yield _sse({"type": "token", "text": text})
            else:
                yield _sse({"type": "reasoning", "text": text})

        # 7) 收尾：脱敏 → 完整答案 → 落库
        raw = "".join(parts)
        sensitive_words = await _load_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        await _save_assistant(answer, citations, hits, gen.model_name)
        yield _sse({
            "type": "done",
            "conversation_id": conversation_id,
            "answer": answer,
            "sensitive_hits": hits,
            "model": gen.model_name,
        })
    except GeneratorExit:
        # 用户点「停止」：保存已生成部分（未脱敏——停止时内容可能不完整，
        # 落库原样留痕；完整答案仅在 done 时脱敏）
        logger.info("问答生成被中止，保存部分答案（conversation=%s）", conversation_id)
        await _save_assistant("".join(parts), citations_holder, [], "unknown")
        raise
    except Exception as exc:
        logger.exception("智能问答异常：%s", exc)
        # 失败也尽量保存已生成部分，便于用户继续
        try:
            await _save_assistant("".join(parts), citations_holder, [], "unknown")
        except Exception:  # pragma: no cover - 兜底不影响主错误上报
            pass
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
