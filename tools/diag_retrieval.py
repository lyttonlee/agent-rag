"""检索链路诊断脚本（2026-10-16，用户排查"相似度低/rerank 不理想"）。

在真实链路上拆解 `_hybrid_retrieve_raw` 每一步，打印原始数据：
  query 向量 → 向量召回(cosine_distance) → 关键词召回(ts_rank_cd)
  → RRF 融合 → rerank 原始 relevance_score → 最终排序

用法：backend/.venv/Scripts/python.exe tools/diag_retrieval.py "<query>"
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.core.security import build_visibility_filter
from app.db.models import Chunk
from app.db.session import AsyncSessionLocal
from app.schemas.contracts import RetrieveRequest
from app.services.model_provider import ModelProvider
from app.services.runtime_config import RetrievalConfig, load_retrieval_config

RRF_K = 60


def _short(s: str, n: int = 48) -> str:
    s = (s or "").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


async def main(query: str) -> None:
    async with AsyncSessionLocal() as db:
        cfg: RetrievalConfig = await load_retrieval_config(db)
        print(f"=== 配置 ===")
        print(f"  vector_top_n={cfg.vector_top_n} keyword_top_n={cfg.keyword_top_n} "
              f"final_top_n={cfg.final_top_n} rerank_enabled={cfg.rerank_enabled} "
              f"vec_distance_threshold={cfg.vec_distance_threshold}")

        req = RetrieveRequest(query=query, max_clearance="TOP_SECRET", rerank=True)

        # ---- 1) query embedding ----
        emb = ModelProvider(role="emb")
        vec = (await emb.embed([query]))[0]
        norm = sum(x * x for x in vec) ** 0.5
        print(f"\n=== 1) query embedding ===\n  维度={len(vec)} L2范数={norm:.4f} "
              f"前5维={[round(x,4) for x in vec[:5]]}")

        vis = build_visibility_filter(
            req.max_clearance, req.caller_scope,
            security_level_col=Chunk.security_level,
            permission_tags_col=Chunk.permission_tags,
        )

        # ---- 2) 向量召回 ----
        vec_q = (
            select(Chunk, Chunk.embedding.cosine_distance(vec).label("dist"))
            .options(joinedload(Chunk.document))
            .where(vis)
            .where(Chunk.granularity == "small")
            .order_by(Chunk.embedding.cosine_distance(vec))
            .limit(cfg.vector_top_n)
        )
        vec_result = (await db.execute(vec_q)).all()
        print(f"\n=== 2) 向量召回 top{len(vec_result)}（cosine_distance，越小越相关）===")
        for i, (c, dist) in enumerate(vec_result, 1):
            print(f"  [{i}] dist={dist:.4f} 《{_short(c.document.title if c.document else '', 30)}》 {_short(c.content)}")
        top_vec_dist = float(vec_result[0][1]) if vec_result else None
        print(f"  → top1 向量距离 = {top_vec_dist}")

        # ---- 3) 关键词召回 ----
        kw_ts = func.plainto_tsquery("chinese", query)
        kw_q = (
            select(Chunk, func.ts_rank_cd(Chunk.search_vec, kw_ts).label("rank"))
            .options(joinedload(Chunk.document))
            .where(vis)
            .where(Chunk.granularity == "small")
            .where(Chunk.search_vec.op("@@")(kw_ts))
            .order_by(func.ts_rank_cd(Chunk.search_vec, kw_ts).desc())
            .limit(cfg.keyword_top_n)
        )
        kw_result = (await db.execute(kw_q)).all()
        print(f"\n=== 3) 关键词召回 top{len(kw_result)}（ts_rank_cd）===")
        for i, (c, rank) in enumerate(kw_result, 1):
            print(f"  [{i}] rank={rank:.4f} 《{_short(c.document.title if c.document else '', 30)}》 {_short(c.content)}")
        print(f"  → keyword_hits = {len(kw_result)}")

        # ---- 4) RRF 融合 ----
        rrf: dict[str, tuple[Chunk, float]] = {}
        for rank, (c, _d) in enumerate(vec_result, 1):
            rrf.setdefault(c.id, (c, 0.0))
            rrf[c.id] = (c, rrf[c.id][1] + 1.0 / (RRF_K + rank))
        for rank, (c, _r) in enumerate(kw_result, 1):
            rrf.setdefault(c.id, (c, 0.0))
            rrf[c.id] = (c, rrf[c.id][1] + 1.0 / (RRF_K + rank))
        fused = sorted(rrf.values(), key=lambda x: x[1], reverse=True)
        top = fused[: cfg.final_top_n]
        print(f"\n=== 4) RRF 融合 top{len(top)}（fused={len(fused)}）===")
        for i, (c, s) in enumerate(top, 1):
            print(f"  [{i}] rrf={s:.5f} {_short(c.content)}")

        # ---- 5) rerank 原始分 ----
        print(f"\n=== 5) rerank（对 top{len(top)} 逐条打分，打印原始 relevance_score）===")
        if not cfg.rerank_enabled:
            print("  rerank 已在配置中心关闭")
            return
        try:
            reranker = ModelProvider(role="rerank")
            docs = [c.content for c, _ in top]
            cap = reranker.settings.rerank_doc_max_chars
            print(f"  rerank_doc_max_chars={cap}（超过则截断后送评）")
            scores = await reranker.rerank(query, docs)
            print(f"  原始 relevance_score 列表（按输入顺序）:")
            for i, s in enumerate(scores):
                clipped = docs[i][:cap] if len(docs[i]) > cap else docs[i]
                print(f"    [{i}] {s:.6f}  len={len(docs[i])}{'(截断→'+str(len(clipped))+')' if len(docs[i])>cap else ''}  {_short(clipped, 70)}")
            print(f"  → min={min(scores):.4f} max={max(scores):.4f} 极差={max(scores)-min(scores):.4f}")
            ranked = sorted(zip(top, scores, strict=True), key=lambda x: x[1], reverse=True)
            print(f"\n=== 6) rerank 后排序 ===")
            for i, ((c, rrf_s), rk_s) in enumerate(ranked, 1):
                print(f"  [{i}] rerank={rk_s:.4f} rrf={rrf_s:.5f} {_short(c.content)}")
        except Exception as exc:
            print(f"  ✗ rerank 调用失败（检索层会降级为 RRF 顺序）：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "电力中长期市场交易规则"
    print(f"查询：{q}\n")
    asyncio.run(main(q))
