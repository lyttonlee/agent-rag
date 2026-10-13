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
from app.services.runtime_config import load_retrieval_config

logger = logging.getLogger(__name__)

_settings = get_settings()
RRF_K = 60


async def hybrid_retrieve(db: AsyncSession, req: RetrieveRequest) -> RetrieveResp:
    # 检索参数从配置中心读取（top_k / 召回宽度 / 重排开关），缺项回退代码默认；
    # 运维在「配置中心 → 检索参数」调整后即时生效，无需重启（用户 2026-10-13 需求）。
    cfg = await load_retrieval_config(db)

    emb = ModelProvider(role="emb")
    query_vec = (await emb.embed([req.query]))[0]

    vis_filter = build_visibility_filter(
        req.max_clearance, req.caller_scope,
        security_level_col=Chunk.security_level,
        permission_tags_col=Chunk.permission_tags,
    )

    # ---- 文档空间过滤（可选）：切片粒度 space_id 下推，免 JOIN documents ----
    # space_ids 为空/缺省 = 不限空间（仍受密级/权限约束）；
    # 传入多个空间 ID 时取并集（知识范围合并检索）。
    space_ids = getattr(req, "space_ids", None)
    space_filter = Chunk.space_id.in_(space_ids) if space_ids else None

    def _apply(q):
        """给召回查询统一叠加 可见性 + 空间 过滤。"""
        q = q.where(vis_filter)
        if space_filter is not None:
            q = q.where(space_filter)
        return q

    # ---- 向量召回 ----
    vec_q = (
        select(Chunk, Chunk.embedding.cosine_distance(query_vec).label("dist"))
        .options(joinedload(Chunk.document))
        .where(vis_filter)
        .order_by(Chunk.embedding.cosine_distance(query_vec))
        .limit(cfg.vector_top_n)
    )
    vec_q = _apply(vec_q)
    # .all() 保留 (Chunk, dist) 元组：既供 RRF 融合取 Chunk，
    # 也供"无有用信息"判定取 top1 向量距离（见文件末尾信号计算）。
    vec_result = (await db.execute(vec_q)).all()
    vec_rows = [row[0] for row in vec_result]

    # ---- 关键词召回（zhparser 中文分词 + ts_rank）----
    kw_ts = func.plainto_tsquery("chinese", req.query)
    kw_q = (
        select(Chunk, func.ts_rank_cd(Chunk.search_vec, kw_ts).label("rank"))
        .options(joinedload(Chunk.document))
        .where(vis_filter)
        .where(Chunk.search_vec.op("@@")(kw_ts))
        .order_by(func.ts_rank_cd(Chunk.search_vec, kw_ts).desc())
        .limit(cfg.keyword_top_n)
    )
    kw_q = _apply(kw_q)
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
    top = fused[: cfg.final_top_n]

    # ---- 重排（在线，best-effort）----
    # 兼容 RetrieveRequest（含 rerank）与 GenerateRequest（无 rerank，默认开启）。
    # rerank 是「增强排序」而非检索必需环节：当其端点慢/抖动/不可用时，
    # 优雅回退到 RRF 融合分排序，保证检索主链路不中断（生产级健壮性）。
    # 全局开关来自配置中心（cfg.rerank_enabled），请求级 rerank=False 同样可关。
    if getattr(req, "rerank", True) and cfg.rerank_enabled and top:
        try:
            reranker = ModelProvider(role="rerank")
            scores = await reranker.rerank(req.query, [c.content for c, _ in top])
            ranked = sorted(zip(top, scores, strict=True), key=lambda x: x[1], reverse=True)
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
    # ---- "无有用信息"判定信号（供 Agent 直答拒答 / 任务型兜底决策）----
    # top_vector_distance：向量路 top1 的余弦距离（已按距离升序，取首个）。
    #   实测标定（Qwen3-Embedding-4B）：主题相关查询 ≈0.28，无关查询 ≈0.64~0.70；
    # keyword_hits：关键词路命中数（zhparser 分词后与语料的词汇重叠度）。
    # 两者需同时异常才判定"无有用信息"（见 agent/policies.is_no_useful_info），
    # 单一脸弱信号误杀率高；rerank 可用时该信号依然有效（距离与分词不依赖 rerank）。
    top_vector_distance = float(vec_result[0][1]) if vec_result else None
    return RetrieveResp(
        query=req.query,
        citations=citations,
        total_matched=len(fused),
        truncated_by_security=False,
        top_vector_distance=top_vector_distance,
        keyword_hits=len(kw_rows),
    )
