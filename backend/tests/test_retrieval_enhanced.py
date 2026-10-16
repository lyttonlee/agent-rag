"""P1+P2 检索增强单测：弱信号判定、候选合并、Citation 构建、父块渲染。

不连数据库——覆盖 retrieve_enhanced 管线中的纯函数部分；
DB 集成（改写重检/父块回表）由 6.49 端到端验证覆盖。
"""

from __future__ import annotations

from app.db.models import Chunk
from app.schemas.contracts import Citation, RetrieveRequest
from app.services.generation_prompt import _render_context
from app.services.retrieval import (
    GRANULARITY_LARGE,
    GRANULARITY_SMALL,
    _apply_rerank_scores,
    _is_weak,
    _merge_raw,
    _RawResult,
    _to_resp,
)
from app.services.runtime_config import RetrievalConfig


def _chunk(cid: str, content: str = "正文") -> Chunk:
    return Chunk(id=cid, document_id="doc1", content=content)


def _raw(n: int = 3, reranked: bool = True, distance: float | None = 0.3, hits: int = 2) -> _RawResult:
    return _RawResult(
        chunks=[(_chunk(f"c{i}"), 0.9 - i * 0.1) for i in range(n)],
        total_matched=n,
        top_vector_distance=distance,
        keyword_hits=hits,
        reranked=reranked,
    )


class TestIsWeak:
    """弱信号三重触发：空结果 / 双信号异常 / rerank top1 低于阈值。"""

    def test_empty_is_weak(self):
        assert _is_weak(_RawResult(), RetrievalConfig())

    def test_strong_is_not_weak(self):
        assert not _is_weak(_raw(), RetrievalConfig())

    def test_double_signal_anomaly(self):
        """距离超阈值 且 关键词零命中 → 弱（与 policies.is_no_useful_info 同款）。"""
        raw = _raw(distance=0.75, hits=0)
        assert _is_weak(raw, RetrievalConfig())

    def test_single_weak_signal_not_weak(self):
        """单信号弱异常不判弱（防误杀，保守策略）。"""
        assert not _is_weak(_raw(distance=0.75, hits=3), RetrievalConfig())
        assert not _is_weak(_raw(distance=0.2, hits=0), RetrievalConfig())

    def test_rerank_below_threshold(self):
        raw = _raw()
        raw.chunks[0] = (raw.chunks[0][0], 0.05)  # top1 rerank 分低于 0.15
        assert _is_weak(raw, RetrievalConfig())

    def test_rrf_score_not_compared_when_not_reranked(self):
        """rerank 降级时（reranked=False）跳过分数信号——RRF 分与 rerank 分不同量纲。"""
        raw = _raw(reranked=False)
        raw.chunks[0] = (raw.chunks[0][0], 0.01)  # RRF 分本来就很小
        assert not _is_weak(raw, RetrievalConfig())

    def test_configurable_threshold(self):
        raw = _raw()
        raw.chunks[0] = (raw.chunks[0][0], 0.1)
        assert _is_weak(raw, RetrievalConfig(parent_inject_min_rerank=0.15))
        assert not _is_weak(raw, RetrievalConfig(parent_inject_min_rerank=0.05))


class TestMergeRaw:
    def test_dedupe_by_chunk_id(self):
        a = _raw(2)
        b = _raw(3)  # c0/c1 与 a 重叠，c2 新增
        merged = _merge_raw(a, b)
        ids = [c.id for c, _ in merged.chunks]
        assert sorted(ids) == ["c0", "c1", "c2"]
        assert merged.total_matched == a.total_matched + b.total_matched

    def test_signal_merge_takes_favorable_side(self):
        a = _RawResult(chunks=[], top_vector_distance=0.7, keyword_hits=0)
        b = _RawResult(chunks=[], top_vector_distance=0.2, keyword_hits=5)
        merged = _merge_raw(a, b)
        assert merged.top_vector_distance == 0.2
        assert merged.keyword_hits == 5


class TestApplyRerankScores:
    """rerank 分数替换的解包正确性（线上实测踩坑回归）。"""

    def test_replaces_scores_and_sorts(self):
        chunks = [(_chunk("c0"), 0.5), (_chunk("c1"), 0.4), (_chunk("c2"), 0.3)]
        out = _apply_rerank_scores(chunks, [0.1, 0.9, 0.5])
        # 按新分降序；旧分被替换（不是嵌套元组）
        assert [c.id for c, _ in out] == ["c1", "c2", "c0"]
        assert [s for _, s in out] == [0.9, 0.5, 0.1]
        for c, _s in out:
            assert isinstance(c, Chunk), "解包后必须是 Chunk，不是 (Chunk, 旧分) 元组"

    def test_empty(self):
        assert _apply_rerank_scores([], []) == []


class TestToResp:
    def test_small_citations_and_granularity(self):
        resp = _to_resp(RetrieveRequest(query="q"), _raw(2))
        assert len(resp.citations) == 2
        assert all(c.granularity == GRANULARITY_SMALL for c in resp.citations)
        assert all(c.content is None for c in resp.citations)
        assert resp.citations[0].snippet  # snippet 仍填充（前端展示）

    def test_injected_parents_appended(self):
        raw = _raw(1)
        raw.injected = [
            Citation(
                chunk_id="p1", document_id="doc1", title="标题", snippet="摘",
                score=0.0, content="父块完整正文", granularity=GRANULARITY_LARGE,
            )
        ]
        resp = _to_resp(RetrieveRequest(query="q"), raw)
        assert len(resp.citations) == 2
        assert resp.citations[-1].granularity == GRANULARITY_LARGE
        assert resp.citations[-1].content == "父块完整正文"

    def test_trace_returned_when_debug(self):
        """debug=True：透出检索过程 trace（排查相似度/rerank 质量）。"""
        raw = _raw(2)
        raw.trace = {"embedding": {"dim": 1024}, "rerank": {"scores": [{"score": 0.9}]}}
        resp = _to_resp(RetrieveRequest(query="q", debug=True), raw)
        assert resp.trace is not None
        assert resp.trace["embedding"]["dim"] == 1024
        assert resp.trace["rerank"]["scores"][0]["score"] == 0.9

    def test_trace_none_when_not_debug(self):
        """默认（debug=False）：不返回 trace，保持生产响应精简。"""
        raw = _raw(2)
        raw.trace = {"embedding": {"dim": 1024}}
        resp = _to_resp(RetrieveRequest(query="q"), raw)
        assert resp.trace is None


class TestRenderContext:
    def test_large_uses_full_content(self):
        """父块注入的大块用完整正文，小块仍用 snippet。"""
        citations = [
            Citation(chunk_id="c1", document_id="d", title="T", snippet="小片段", score=0.9),
            Citation(
                chunk_id="p1", document_id="d", title="T", snippet="摘",
                score=0.0, content="父块完整正文内容", granularity=GRANULARITY_LARGE,
            ),
        ]
        ctx = _render_context(citations)
        assert "小片段" in ctx
        assert "父块完整正文内容" in ctx

    def test_empty_citations(self):
        assert _render_context([]) == "（无可用检索材料）"
