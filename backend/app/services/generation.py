"""推理生成：检索增强 + 强制引用溯源 + 敏感词后处理 + 流式输出。

设计铁律：
- 每处事实性内容必须带引用角标；无依据时必须明确提示，禁止静默编造。
- 生成后再做敏感词掩码，并将命中回报第三方（sensitive_hits）。
- 流式输出：先回传检索元数据（citations），再逐 token 流式返回正文，
  避免大草稿在等待期间被前端判定为"超时/假死"。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.db.models import ConfigKV
from app.schemas.contracts import Citation, GenerateRequest
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
      {"type":"meta","model":...,"citations":[...]}        检索完成，先给上下文
      {"type":"token","text":"..."}                         正文逐片
      {"type":"reasoning","text":"..."}                     思考链（可选）
      {"type":"done","answer":...,"sensitive_hits":[...]}    收尾（含敏感词脱敏）
      {"type":"error","message":"..."}                      异常
    """
    try:
        # 1) 检索上下文（含 rerank 优雅降级）
        retrieve_resp = await hybrid_retrieve(db, req)  # type: ignore[arg-type]
        citations: list[Citation] = retrieve_resp.citations
        gen = ModelProvider(role="gen")
        messages = build_generation_messages(req, citations)

        # 2) 先回传检索元数据，前端可立即渲染"参考材料"
        yield _sse({
            "type": "meta",
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

        # 3) 流式生成（思考链 + 正文）
        buf = []
        async for kind, text in gen.generate_stream(messages):
            buf.append(text if kind == "content" else "")
            if kind == "content":
                yield _sse({"type": "token", "text": text})
            else:
                yield _sse({"type": "reasoning", "text": text})

        # 4) 收尾：敏感词脱敏后回传完整答案
        raw = "".join(buf)
        sensitive_words = await _resolve_sensitive_words(db, req)
        answer, hits = redact_sensitive(raw, sensitive_words)
        yield _sse({
            "type": "done",
            "answer": answer,
            "sensitive_hits": hits,
            "model": gen.model_name,
        })
    except Exception as exc:  # 不崩溃连接，向前端推送错误事件
        logger.exception("流式生成异常：%s", exc)
        yield _sse({"type": "error", "message": f"{type(ex).__name__}: {str(ex)[:300]}"})
