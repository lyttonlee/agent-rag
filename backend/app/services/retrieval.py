"""混合检索：tsvector(BM25) + pgvector(HNSW) → RRF 融合 → 重排 → 权限下推。

链路（详见《详细功能设计文档》检索章节）：
  关键词召回 tsvector+zhparser(top-N，仅 granularity='small')
  向量召回 pgvector HNSW(top-N，仅 granularity='small')
  → RRF 融合
  → Qwen3-Reranker-0.6B 重排(top-50)
  → 权限/密级下推过滤（服务端 WHERE）

P1+P2 增强入口 ``retrieve_enhanced``（2026-10-13，检索/问答/起草统一走此函数）：
  1. ``_hybrid_retrieve_raw``  基础混合检索（小块粒度）
  2. query 改写重检（P2，services/query_rewrite.py）：弱信号触发 →
     S1 同义扩展 / S2 HyDE / S3 子问题拆解 → 候选并集对原 query 重排
  3. 条件父块注入（P1）：改写后仍弱 → 按 parent_id 回表取大块
     （权限二次校验 + 去重 + token 预算），注入生成上下文

三级兜底顺序即用户需求优先级：先改写救回，再注入大块补上下文，
仍不足才拒答/任务型兜底（agent/policies.py）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.security import build_visibility_filter
from app.db.models import Chunk
from app.schemas.contracts import Citation, RetrieveRequest, RetrieveResp
from app.services.model_provider import ModelProvider
from app.services.query_rewrite import rewrite_queries
from app.services.runtime_config import RetrievalConfig, load_retrieval_config

logger = logging.getLogger(__name__)

_settings = get_settings()
RRF_K = 60

# 父子块粒度常量（与迁移 0008 / models.Chunk.granularity 一致）
GRANULARITY_SMALL = "small"
GRANULARITY_LARGE = "large"


@dataclass
class _RawResult:
    """一次混合检索的原始结果（重排后、构建 Citation 前）。

    chunks: [(Chunk, score)] 按分数降序；reranked 标记分数是否为 rerank
    归一化分（False 时为 RRF 融合分，两者量纲不同，阈值判定需区分）。
    injected: 条件父块注入的大块 Citation（P1；空列表=未注入）。
    """

    chunks: list[tuple[Chunk, float]] = field(default_factory=list)
    total_matched: int = 0
    top_vector_distance: float | None = None
    keyword_hits: int = 0
    reranked: bool = False
    injected: list[Citation] = field(default_factory=list)
    # 检索过程调试信息（embedding/召回/融合/rerank 原始分/耗时）；
    # debug=True 时经 _to_resp 透出到页面，方便排查"相似度低/rerank 不理想"
    trace: dict = field(default_factory=dict)


async def _hybrid_retrieve_raw(
    db: AsyncSession, req: RetrieveRequest, cfg: RetrievalConfig
) -> _RawResult:
    """基础混合检索（仅检索小块 granularity='small'，返回原始结果）。

    逐阶段记录 trace（debug=True 时透出到页面）并打日志，用于排查
    "相似度低 / rerank 不理想"：query 向量维度与范数、向量召回 top-N
    余弦距离、关键词召回 top-N、RRF 融合分、rerank 模型返回的**原始
    relevance_score**（逐条，含降级标记与耗时）、各阶段耗时。
    """
    trace: dict = {
        "query": req.query,
        "config": {
            "vector_top_n": cfg.vector_top_n,
            "keyword_top_n": cfg.keyword_top_n,
            "final_top_n": cfg.final_top_n,
            "rerank_enabled": cfg.rerank_enabled,
            "vec_distance_threshold": cfg.vec_distance_threshold,
            "rerank_doc_max_chars": getattr(_settings, "rerank_doc_max_chars", None),
        },
    }
    timing: dict = {}

    # ---- query embedding ----
    emb = ModelProvider(role="emb")
    t0 = time.monotonic()
    query_vec = (await emb.embed([req.query]))[0]
    timing["embedding_ms"] = round((time.monotonic() - t0) * 1000, 1)
    l2 = sum(x * x for x in query_vec) ** 0.5
    trace["embedding"] = {
        "model": emb.model_name, "dim": len(query_vec),
        "l2_norm": round(l2, 4), "latency_ms": timing["embedding_ms"],
    }
    logger.debug(
        "检索[embedding] model=%s dim=%d l2=%.4f %.1fms",
        emb.model_name, len(query_vec), l2, timing["embedding_ms"],
    )

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
        """给召回查询统一叠加 可见性 + 空间 + 粒度 过滤。"""
        q = q.where(vis_filter)
        # 粒度过滤：大块（large）无向量/tsvector，必须显式排除——
        # 否则向量路 ORDER BY 会把 NULL 向量的大块行带进结果尾部。
        q = q.where(Chunk.granularity == GRANULARITY_SMALL)
        if space_filter is not None:
            q = q.where(space_filter)
        return q

    # ---- 向量召回 ----
    t0 = time.monotonic()
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
    timing["vector_ms"] = round((time.monotonic() - t0) * 1000, 1)
    vec_rows = [row[0] for row in vec_result]
    trace["vector_recall"] = [
        {
            "rank": i, "chunk_id": c.id, "dist": round(float(d), 4),
            "title": (c.document.title if c.document else ""),
            "snippet": c.content[:80],
        }
        for i, (c, d) in enumerate(vec_result, 1)
    ]
    logger.debug(
        "检索[向量召回] top%d 余弦距离(升序): %s",
        len(vec_result), [round(float(d), 4) for _, d in vec_result[:10]],
    )

    # ---- 关键词召回（zhparser 中文分词 + ts_rank）----
    t0 = time.monotonic()
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
    # .all() 保留 (Chunk, ts_rank)：ts_rank 仅入 trace 供排查，
    # RRF 融合仍按召回序号计权（与旧行为一致）。
    kw_result = (await db.execute(kw_q)).all()
    timing["keyword_ms"] = round((time.monotonic() - t0) * 1000, 1)
    kw_rows = [row[0] for row in kw_result]
    trace["keyword_recall"] = [
        {
            "rank": i, "chunk_id": c.id, "ts_rank": round(float(r), 4),
            "title": (c.document.title if c.document else ""),
            "snippet": c.content[:80],
        }
        for i, (c, r) in enumerate(kw_result, 1)
    ]
    logger.debug(
        "检索[关键词召回] top%d ts_rank(降序): %s",
        len(kw_result), [round(float(r), 4) for _, r in kw_result[:10]],
    )

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
    trace["rrf_fusion"] = [
        {"rank": i, "chunk_id": c.id, "rrf": round(s, 5), "snippet": c.content[:80]}
        for i, (c, s) in enumerate(top, 1)
    ]
    trace["fused_count"] = len(fused)
    logger.debug(
        "检索[RRF融合] fused=%d top%d rrf(降序): %s",
        len(fused), len(top), [round(s, 5) for _, s in top[:10]],
    )

    # ---- 重排（在线，best-effort）----
    # 兼容 RetrieveRequest（含 rerank）与 GenerateRequest（无 rerank，默认开启）。
    # rerank 是「增强排序」而非检索必需环节：当其端点慢/抖动/不可用时，
    # 优雅回退到 RRF 融合分排序，保证检索主链路不中断（生产级健壮性）。
    # 全局开关来自配置中心（cfg.rerank_enabled），请求级 rerank=False 同样可关。
    reranked = False
    rr_on = bool(getattr(req, "rerank", True) and cfg.rerank_enabled and top)
    rr_trace: dict = {
        "enabled": rr_on, "model": None, "degraded": False, "latency_ms": None,
        "doc_max_chars": getattr(_settings, "rerank_doc_max_chars", None), "scores": [],
    }
    if rr_on:
        try:
            t0 = time.monotonic()
            reranker = ModelProvider(role="rerank")
            rr_trace["model"] = reranker.model_name
            scores = await reranker.rerank(req.query, [c.content for c, _ in top])
            rr_trace["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            ranked = sorted(zip(top, scores, strict=True), key=lambda x: x[1], reverse=True)
            reranked = True
            # 逐条记录模型返回的原始 relevance_score（排查 rerank 质量的关键）
            rr_trace["scores"] = [
                {
                    "index": i, "score": round(float(s), 4), "chunk_id": c.id,
                    "snippet": c.content[:80],
                }
                for i, ((c, _), s) in enumerate(zip(top, scores, strict=True))
            ]
            logger.info(
                "检索[rerank] model=%s %.1fms 原始 relevance_score(降序): %s",
                reranker.model_name, rr_trace["latency_ms"],
                [round(float(s), 4) for s in sorted(scores, reverse=True)],
            )
        except Exception as exc:  # 重排服务不可用：回退 RRF 顺序，不阻断检索
            logger.warning("rerank 调用失败，回退 RRF 顺序：%s", exc)
            rr_trace["degraded"] = True
            rr_trace["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            # 与成功分支保持同一形状：((Chunk, rrf_score), 展示分)
            ranked = [((c, s_rrf), s_rrf) for (c, s_rrf) in top]
    else:
        # 不做重排：以 RRF 融合分作为展示分，保持与上面分支一致的形状
        # ((Chunk, rrf_score), display_score)
        ranked = [((c, s), s) for c, s in top]
        rr_trace["note"] = "rerank 未启用（配置中心关闭 / 请求关闭 / 无候选）"
    trace["rerank"] = rr_trace

    # ---- "无有用信息"判定信号（供 Agent 直答拒答 / 任务型兜底决策）----
    # top_vector_distance：向量路 top1 的余弦距离（已按距离升序，取首个）。
    #   实测标定（Qwen3-Embedding-4B）：主题相关查询 ≈0.28，无关查询 ≈0.64~0.70；
    # keyword_hits：关键词路命中数（zhparser 分词后与语料的词汇重叠度）。
    # 两者需同时异常才判定"无有用信息"（见 agent/policies.is_no_useful_info），
    # 单一脸弱信号误杀率高；rerank 可用时该信号依然有效（距离与分词不依赖 rerank）。
    top_vector_distance = float(vec_result[0][1]) if vec_result else None
    trace["top_vector_distance"] = top_vector_distance
    trace["keyword_hits"] = len(kw_rows)
    trace["timing_ms"] = timing
    trace["final"] = [
        {
            "rank": i, "chunk_id": c.id, "score": round(float(s), 4),
            "title": (c.document.title if c.document else ""),
            "snippet": c.content[:80],
        }
        for i, ((c, _), s) in enumerate(ranked, 1)
    ]
    logger.info(
        "检索完成 q=%.48s | 向量top1距离=%s 关键词命中=%d | rerank降级=%s | top1分=%.4f",
        req.query, top_vector_distance, len(kw_rows), rr_trace["degraded"],
        (ranked[0][1] if ranked else 0.0),
    )
    return _RawResult(
        chunks=[(c, float(s)) for (c, _), s in ranked],
        total_matched=len(fused),
        top_vector_distance=top_vector_distance,
        keyword_hits=len(kw_rows),
        reranked=reranked,
        trace=trace,
    )


def _is_weak(raw: _RawResult, cfg: RetrievalConfig) -> bool:
    """弱信号判定：改写与父块注入的共同触发条件（任一命中即弱）。

    三信号（对应《检索优化方案》三重触发）：
    1. 结果为空；
    2. 双信号异常：向量 top1 距离超阈值 且 关键词零命中（与
       agent/policies.is_no_useful_info 同款保守判定，防误杀）；
    3. rerank top1 低于阈值（有命中但都弱相关——改写的主场景）。
       rerank 降级时（reranked=False）跳过该信号（RRF 分与 rerank 分
       不同量纲，不可比）。
    """
    if not raw.chunks:
        return True
    if (
        raw.top_vector_distance is not None
        and raw.keyword_hits == 0
        and raw.top_vector_distance > cfg.vec_distance_threshold
    ):
        return True
    if raw.reranked and raw.chunks[0][1] < cfg.parent_inject_min_rerank:
        return True
    return False


def _apply_rerank_scores(
    chunks: list[tuple[Chunk, float]], scores: list[float]
) -> list[tuple[Chunk, float]]:
    """用 rerank 新分替换旧分并降序排列（纯函数）。

    独立出来以便单测：chunks 元素是 (Chunk, 旧分)，zip 后是
    ((Chunk, 旧分), 新分)——解包时必须剥掉旧分层，否则产出嵌套元组，
    下游 _to_resp 取 .id 时会 AttributeError（线上实测踩坑）。
    """
    ranked = sorted(zip(chunks, scores, strict=True), key=lambda x: x[1], reverse=True)
    return [(c, float(s)) for (c, _old), s in ranked]


async def _rerank_raw(
    db: AsyncSession, req: RetrieveRequest, raw: _RawResult, cfg: RetrievalConfig
) -> _RawResult:
    """对原始结果按原 query 重排（改写候选合并后调用，保证分数同量纲）。"""
    if not raw.chunks or not (getattr(req, "rerank", True) and cfg.rerank_enabled):
        return raw
    try:
        reranker = ModelProvider(role="rerank")
        scores = await reranker.rerank(req.query, [c.content for c, _ in raw.chunks])
        return _RawResult(
            chunks=_apply_rerank_scores(raw.chunks, scores),
            total_matched=raw.total_matched,
            top_vector_distance=raw.top_vector_distance,
            keyword_hits=raw.keyword_hits,
            reranked=True,
        )
    except Exception as exc:
        logger.warning("合并候选重排失败，保留融合序：%s", exc)
        return raw


def _merge_raw(a: _RawResult, b: _RawResult) -> _RawResult:
    """合并两次检索的候选（按 chunk_id 去重，保留先出现者的分数）。

    合并后由 ``_rerank_raw`` 对原 query 重新打分，此处分数仅作占位；
    距离/关键词信号取对判定更有利的一边（距离取最小、命中取最大）。
    """
    seen: dict[str, tuple[Chunk, float]] = {}
    for c, s in a.chunks + b.chunks:
        seen.setdefault(c.id, (c, s))
    dist = _min_none(a.top_vector_distance, b.top_vector_distance)
    return _RawResult(
        chunks=list(seen.values()),
        total_matched=a.total_matched + b.total_matched,
        top_vector_distance=dist,
        keyword_hits=max(a.keyword_hits, b.keyword_hits),
        reranked=False,
    )


def _min_none(x: float | None, y: float | None) -> float | None:
    vals = [v for v in (x, y) if v is not None]
    return min(vals) if vals else None


async def _rewrite_and_retry(
    db: AsyncSession, req: RetrieveRequest, raw: _RawResult, cfg: RetrievalConfig
) -> _RawResult:
    """P2：弱信号触发 query 改写重检（降级红线：任何失败保留原结果）。

    流程：生成改写变体（S1 同义扩展/S2 HyDE/S3 子问题拆解，见
    query_rewrite.py）→ 逐变体检索（受 rewrite_max_rounds 与总超时约束）
    → 候选并集对**原 query** 重排（分数同量纲）→ 仍弱则返回合并结果
    （由调用方决定是否进入父块注入）。
    """
    started = time.monotonic()
    try:
        variants = await asyncio.wait_for(
            rewrite_queries(req.query, cfg.rewrite_strategy), timeout=cfg.rewrite_timeout_ms / 1000
        )
    except Exception as exc:  # 改写生成失败/超时：保留原结果，不阻断
        logger.warning("query 改写生成失败，保留原结果：%s", exc)
        return raw

    if not variants:
        return raw

    merged = raw
    done_rounds = 0
    for variant in variants[: cfg.rewrite_max_rounds]:
        # 总超时预算耗尽即停（剩余变体不再尝试）——但**至少保证完成一轮**
        # 重检：否则变体生成耗时（如 auto 双路 ≈22s）会吃掉全部预算，
        # 导致零轮次检索，生成开销全浪费。
        if done_rounds > 0 and (time.monotonic() - started) * 1000 > cfg.rewrite_timeout_ms:
            logger.info("query 改写超时预算耗尽，使用已合并候选")
            break
        vreq = req.model_copy(update={"query": variant})
        vraw = await _hybrid_retrieve_raw(db, vreq, cfg)
        merged = _merge_raw(merged, vraw)
        done_rounds += 1

    # 对原 query 重新打分（合并候选分数同量纲），截回 final_top_n
    merged = await _rerank_raw(db, req, merged, cfg)
    merged.chunks = merged.chunks[: cfg.final_top_n]
    # 保留首次检索全过程 trace，并追加改写段（改写后的最终排序见 citations）
    merged.trace = {
        **raw.trace,
        "rewrite": {
            "triggered": True,
            "strategy": cfg.rewrite_strategy,
            "variants": list(variants[: cfg.rewrite_max_rounds]),
            "rounds": done_rounds,
            "merged_total": merged.total_matched,
        },
    }
    logger.info(
        "query 改写重检：strategy=%s variants=%d rounds=%d 合并候选=%d",
        cfg.rewrite_strategy, len(variants), done_rounds, merged.total_matched,
    )
    return merged


async def _inject_parents(
    db: AsyncSession, req: RetrieveRequest, raw: _RawResult, cfg: RetrievalConfig
) -> _RawResult:
    """P1：条件父块注入——命中小块后按 parent_id 回表取大块补上下文。

    设计红线：
    1. **权限二次校验**：父块查询复用同一 visibility filter（父块行冗余了
       密级/权限列，但注入是独立链路，必须独立校验，防入库逻辑未来变动
       引入越权）；
    2. 去重：top_k 个小块常来自同一父块，按 parent_id 去重；
    3. 预算：父块数 ≤ parent_inject_max_count、总 token ≤
       parent_inject_max_tokens（超预算按相关度截断），防生成上下文膨胀；
    4. 父块 Citation 携带完整正文（content 字段），生成提示词优先用它
       而非 200 字 snippet——否则注入失去意义。
    """
    parent_ids: list[str] = []
    for c, _score in raw.chunks:
        pid = getattr(c, "parent_id", None)
        if pid and pid not in parent_ids:
            parent_ids.append(pid)
        if len(parent_ids) >= cfg.parent_inject_max_count:
            break
    if not parent_ids:
        return raw

    vis_filter = build_visibility_filter(
        req.max_clearance, req.caller_scope,
        security_level_col=Chunk.security_level,
        permission_tags_col=Chunk.permission_tags,
    )
    # 父块回表：主键 IN + 权限过滤 + 粒度校验（三保险）
    parents = (
        (
            await db.execute(
                select(Chunk)
                .options(joinedload(Chunk.document))
                .where(
                    Chunk.id.in_(parent_ids),
                    Chunk.granularity == GRANULARITY_LARGE,
                    vis_filter,
                )
            )
        )
        .scalars()
        .all()
    )
    if not parents:
        return raw

    # 按相关度（对应命中小块的分数）排序 + token 预算截断
    rank_of = {pid: i for i, pid in enumerate(parent_ids)}
    parents = sorted(parents, key=lambda p: rank_of.get(p.id, 99))
    kept: list[Chunk] = []
    budget = cfg.parent_inject_max_tokens
    for p in parents:
        # 中文 1 字≈1 token（chonkie/Qwen 实测密度），用字符数近似计量
        cost = len(p.content)
        if kept and cost > budget:
            break
        kept.append(p)
        budget -= cost

    injected = [
        Citation(
            chunk_id=p.id,
            document_id=p.document_id,
            title=p.document.title if p.document else "",
            doc_no=p.document.doc_no if p.document else None,
            snippet=p.content[:200],
            score=0.0,  # 父块不参与相关度排序，仅作上下文补充
            content=p.content,       # 完整正文：生成提示词优先使用
            granularity=GRANULARITY_LARGE,
        )
        for p in kept
    ]
    logger.info(
        "父块注入：命中小块 %d → 注入大块 %d（预算剩余 %d token）",
        len(raw.chunks), len(kept), max(budget, 0),
    )
    return _RawResult(
        chunks=raw.chunks,  # 小块保持检索序（引用展示/拒答判定不变）
        total_matched=raw.total_matched,
        top_vector_distance=raw.top_vector_distance,
        keyword_hits=raw.keyword_hits,
        reranked=raw.reranked,
        injected=injected,
        trace={
            **raw.trace,
            "parent_inject": {
                "triggered": True, "injected_count": len(kept),
                "budget_left": max(budget, 0),
            },
        },
    )


def _to_resp(req: RetrieveRequest, raw: _RawResult) -> RetrieveResp:
    """原始结果 → 响应 Model（父块注入的 Citation 追加在尾部）。

    req.debug=True 时附带 trace（各阶段召回/融合/rerank 原始分/耗时），
    供前端排查检索质量；默认不返回，保持生产响应精简。
    """
    citations = [
        Citation(
            chunk_id=c.id,
            document_id=c.document_id,
            title=c.document.title if c.document else "",
            doc_no=c.document.doc_no if c.document else None,
            snippet=c.content[:200],
            score=float(s),
            granularity=GRANULARITY_SMALL,
        )
        for c, s in raw.chunks
    ]
    citations.extend(raw.injected)
    return RetrieveResp(
        query=req.query,
        citations=citations,
        total_matched=raw.total_matched,
        truncated_by_security=False,
        top_vector_distance=raw.top_vector_distance,
        keyword_hits=raw.keyword_hits,
        trace=raw.trace if getattr(req, "debug", False) else None,
    )


async def hybrid_retrieve(db: AsyncSession, req: RetrieveRequest) -> RetrieveResp:
    """基础混合检索入口（无改写/注入，供测试与内部调用）。"""
    cfg = await load_retrieval_config(db)
    raw = await _hybrid_retrieve_raw(db, req, cfg)
    return _to_resp(req, raw)


async def retrieve_enhanced(db: AsyncSession, req: RetrieveRequest) -> RetrieveResp:
    """P1+P2 增强检索入口（检索路由 / 问答 / 起草统一使用）。

    顺序：基础检索 →（弱信号）query 改写重检 →（仍弱）条件父块注入。
    每一步都可被配置中心独立关闭；任何一步异常只降级不抛出
    （改写/注入是增强，不是主链路依赖）。

    trace（req.debug=True 时透出）：记录弱信号判定依据、是否触发改写/
    父块注入，配合 _hybrid_retrieve_raw 的各阶段明细，完整还原一次检索，
    便于定位"相似度低 / rerank 不理想"出在哪一步。
    """
    cfg = await load_retrieval_config(db)
    raw = await _hybrid_retrieve_raw(db, req, cfg)

    # 弱信号判定留痕：解释"为何（未）触发改写 / 父块注入"
    weak = _is_weak(raw, cfg)
    raw.trace["weak_signal"] = {
        "is_weak": weak,
        "top_vector_distance": raw.top_vector_distance,
        "keyword_hits": raw.keyword_hits,
        "reranked": raw.reranked,
        "rerank_top1": (
            round(float(raw.chunks[0][1]), 4) if (raw.chunks and raw.reranked) else None
        ),
        "vec_distance_threshold": cfg.vec_distance_threshold,
        "parent_inject_min_rerank": cfg.parent_inject_min_rerank,
    }

    if cfg.rewrite_enabled and weak:
        raw = await _rewrite_and_retry(db, req, raw, cfg)

    if cfg.parent_inject_enabled and _is_weak(raw, cfg):
        raw = await _inject_parents(db, req, raw, cfg)

    return _to_resp(req, raw)
