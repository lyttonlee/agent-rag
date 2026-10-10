"""推理生成：检索增强 + 强制引用溯源 + 敏感词后处理。

设计铁律：
- 每处事实性内容必须带引用角标；无依据时必须明确提示，禁止静默编造。
- 生成后再做敏感词掩码，并将命中回报第三方（sensitive_hits）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import redact_sensitive
from app.schemas.contracts import Citation, GenerateRequest, GenerateResp
from app.services.generation_prompt import build_generation_messages
from app.services.model_provider import ModelProvider
from app.services.retrieval import hybrid_retrieve


async def generate_answer(db: AsyncSession, req: GenerateRequest) -> GenerateResp:
    # 1) 检索上下文
    retrieve_resp = await hybrid_retrieve(db, req)  # type: ignore[arg-type]
    citations: list[Citation] = retrieve_resp.citations

    # 2) 构造带引用约束的提示词
    gen = ModelProvider(role="gen")
    messages = build_generation_messages(req, citations)

    raw = await gen.generate(messages)

    # 3) 敏感词后处理（请求级覆盖 > 配置中心词库）
    sensitive_words = req.sensitive_words or []
    answer, hits = redact_sensitive(raw, sensitive_words)

    return GenerateResp(
        answer=answer,
        citations=citations,
        sensitive_hits=hits,
        model=gen.model_name,
        truncated_by_security=retrieve_resp.truncated_by_security,
    )
