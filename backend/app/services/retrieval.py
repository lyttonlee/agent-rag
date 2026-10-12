"""混合检索：tsvector(BM25) + pgvector(HNSW) → RRF 融合 → 重排 → 权限下推。

链路（详见《详细功能设计文档》检索章节）：
  关键词召回 tsvector+zhparser(top-N)
  向量召回 pgvector HNSW(top-N)
  → RRF 融合
  → Qwen3-Reranker-0.6B 重排(top-50)
  → 权限/密级下推过滤（服务端 WHERE）
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.security import build_visibility_filter
from app.db.models import Chunk
from app.schemas.contracts import Citation, RetrieveRequest, RetrieveResp
from app.services.model_provider import ModelProvider

logger = logging.getLogger(__name__)

_settings = get_settings()
RRF_K = 60


async def hybrid_retrieve(db: AsyncSession, req: RetrieveRequest) -> RetrieveResp:
    emb = ModelProvider(role="emb")
    query_vec = (await emb.embed([req.query]))[0]

    vis_filter = build_visibility_filter(
        req.max_clearance, req.caller_scope,
        security_level_col=Chunk.security_level,
        permission_tags_col=Chunk.permission_tags,
    )

    # ---- 向量召回 ----
    vec_q = (
        select(Chunk, Chunk.embedding.cosine_distance(query_vec).label("dist"))
        .options(joinedload(Chunk.document))
        .where(vis_filter)
        .order_by(Chunk.embedding.cosine_distance(query_vec))
        .limit(_settings.retrieve_vector_top_n)
    )
    vec_rows = (await db.execute(vec_q)).scalars().all()

    # ---- 关键词召回（zhparser 中文分词 + ts_rank）----
    kw_ts = func.plainto_tsquery("chinese", req.query)
    kw_q = (
        select(Chunk, func.ts_rank_cd(Chunk.search_vec, kw_ts).label("rank"))
        .options(joinedload(Chunk.document))
        .where(vis_filter)
        .where(Chunk.search_vec.op("@@")(kw_ts))
        .order_by(func.ts_rank_cd(Chunk.search_vec, kw_ts).desc())
        .limit(_settings.retrieve_keyword_top_n)
    )
    kw_rows = (await db.execute(kw_q)).scalars().all()

    # ---- RRF 融合 ----
    rrf: dict[str, tuple[Chunk, float]] = {}
    for rank, c in enumerate(vec_rows, start=1):
        rrf.setdefault(c.id, (c, 0.0))
        rrf[c.id] = (c, rrf[c.id][1] + 1.0 / (RRF_K + rank))
    for rank, c in enumerate(kw_rows, start=1):
        rrf.setdefault(c.id, (c, 0.0))
        rrf[c.id] = (c, rrf[c.id][1] + 1.0 / (RRF_K + rank))

    fused = sorted(rrf.values(), key=lambda x: x[1], reverse=True)
    top = fused[: _settings.retrieve_final_top_n]

    # ---- 重排（在线，best-effort）----
    # 兼容 RetrieveRequest（含 rerank）与 GenerateRequest（无 rerank，默认开启）。
    # rerank 是「增强排序」而非检索必需环节：当其端点慢/抖动/不可用时，
    # 优雅回退到 RRF 融合分排序，保证检索主链路不中断（生产级健壮性）。
    if getattr(req, "rerank", True) and top:
        try:
            reranker = ModelProvider(role="rerank")
            scores = await reranker.rerank(req.query, [c.content for c, _ in top])
            ranked = sorted(zip(top, scores), key=lambda x: x[1], reverse=True)
        except Exception as exc:  # 重排服务不可用：回退 RRF 顺序，不阻断检索
            logger.warning("rerank 调用失败，回退 RRF 顺序：%s", exc)
            # 与成功分支保持同一形状：((Chunk, rrf_score), 展示分)
            ranked = [((c, s_rrf), s_rrf) for (c, s_rrf) in top]
    else:
        # 不做重排：以 RRF 融合分作为展示分，保持与上面分支一致的形状
        # ((Chunk, rrf_score), display_score)
        ranked = [((c, s), s) for c, s in top]

    citations = [
        Citation(
            chunk_id=c.id,
            document_id=c.document_id,
            title=c.document.title if c.document else "",
            doc_no=c.document.doc_no if c.document else None,
            snippet=c.content[:200],
            score=float(s),
        )
        for (c, _), s in ranked
    ]
    return RetrieveResp(
        query=req.query,
        citations=citations,
        total_matched=len(fused),
        truncated_by_security=False,
    )
