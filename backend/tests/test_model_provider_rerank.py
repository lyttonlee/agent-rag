"""ModelProvider.rerank 文档截断回归测试（2026-10-13 实测踩坑）。

背景：内网 Qwen3-Reranker-0.6B 最大上下文 4096 tokens，存量文档存在
「整篇 1 个 chunk」的长块（实测最长 5760 字），直接投喂被服务端
HTTP 400 确定性拒绝，且长文打分耗时劣化（单文档 ~38s）。修复：
送评前按 settings.rerank_doc_max_chars 截断（默认 1200 字）。

本组测试不连真实模型服务，mock httpx 捕获请求体，断言：
1. 超长文档被截断到配置上限；
2. 短文档原样透传（不篡改）；
3. 截断不改变文档数量与 index 对齐（scores 按输入顺序返回）；
4. top_n 截断逻辑按分数降序生效。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.model_provider import ModelProvider


class _FakeResponse:
    """最小 httpx.Response 替身：仅实现 raise_for_status/json。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_provider(max_chars: int = 1200) -> ModelProvider:
    """构造 rerank Provider（不发起任何网络连接）。"""
    settings = Settings(rerank_doc_max_chars=max_chars)
    return ModelProvider(
        role="rerank",
        base_url="http://127.0.0.1:9/v1",  # 占位，测试中不会真连
        api_key="EMPTY",
        model_name="fake-reranker",
        settings=settings,
    )


def _patch_post(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """patch httpx.AsyncClient.post：捕获请求体并按 index 回造响应。"""

    async def fake_post(self: Any, url: str, json: dict[str, Any] | None = None, **kw: Any):
        captured["url"] = url
        captured["payload"] = json
        docs = json["documents"] if json else []
        # 响应按原始输入顺序给出（vLLM 风格），分数用序号倒序造数据
        results = [
            {"index": i, "document": {"text": d}, "relevance_score": 1.0 - i * 0.1}
            for i, d in enumerate(docs)
        ]
        return _FakeResponse({"results": results})

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)


class TestRerankTruncation:
    """rerank 送评文档截断（规避 4096 tokens 上限与长文耗时劣化）。"""

    @pytest.mark.asyncio
    async def test_long_doc_truncated_to_cap(self, monkeypatch: pytest.MonkeyPatch):
        """超长文档截断到 rerank_doc_max_chars，不超限。"""
        provider = _make_provider(max_chars=1200)
        long_doc = "汛" * 5760  # 实测存量最长块 5760 字
        captured: dict[str, Any] = {}
        _patch_post(monkeypatch, captured)

        scores = await provider.rerank("查询", [long_doc])

        sent = captured["payload"]["documents"]
        assert len(sent) == 1, "截断不改变文档数量"
        assert len(sent[0]) == 1200, "超长文档必须截断到配置上限"
        assert len(scores) == 1 and scores[0] == 1.0

    @pytest.mark.asyncio
    async def test_short_doc_passthrough(self, monkeypatch: pytest.MonkeyPatch):
        """短文档原样透传，不被截断污染。"""
        provider = _make_provider(max_chars=1200)
        doc = "值班人员应当如实填写值班记录。"
        captured: dict[str, Any] = {}
        _patch_post(monkeypatch, captured)

        await provider.rerank("查询", [doc])

        assert captured["payload"]["documents"] == [doc], "短文档必须原样送评"

    @pytest.mark.asyncio
    async def test_scores_align_by_input_order(self, monkeypatch: pytest.MonkeyPatch):
        """响应乱序返回时，分数必须按输入 index 对齐回原顺序。"""
        provider = _make_provider()
        docs = ["甲文档", "乙文档", "丙文档"]
        captured: dict[str, Any] = {}

        async def fake_post(self: Any, url: str, json: dict[str, Any] | None = None, **kw: Any):
            captured["payload"] = json
            # 乱序返回：index 2 最高分
            return _FakeResponse(
                {
                    "results": [
                        {"index": 2, "document": {"text": docs[2]}, "relevance_score": 0.9},
                        {"index": 0, "document": {"text": docs[0]}, "relevance_score": 0.1},
                        {"index": 1, "document": {"text": docs[1]}, "relevance_score": 0.5},
                    ]
                }
            )

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        scores = await provider.rerank("查询", docs)

        assert scores == [0.1, 0.5, 0.9], "分数必须按输入顺序对齐，而非响应顺序"

    @pytest.mark.asyncio
    async def test_top_n_sorts_desc(self, monkeypatch: pytest.MonkeyPatch):
        """top_n 按分数降序截断（修复历史死代码：原实现在 raise 之后不可达）。"""
        provider = _make_provider()
        captured: dict[str, Any] = {}
        _patch_post(monkeypatch, captured)

        scores = await provider.rerank("查询", ["a", "b", "c"], top_n=2)

        # mock 造分：1.0 / 0.9 / 0.8，降序取前 2
        assert scores == [1.0, 0.9]

    @pytest.mark.asyncio
    async def test_boundary_doc_exact_cap(self, monkeypatch: pytest.MonkeyPatch):
        """恰好等于上限的文档不被截断（边界不误杀）。"""
        provider = _make_provider(max_chars=1200)
        doc = "x" * 1200
        captured: dict[str, Any] = {}
        _patch_post(monkeypatch, captured)

        await provider.rerank("查询", [doc])

        assert captured["payload"]["documents"][0] == doc

    @pytest.mark.asyncio
    async def test_http_error_propagates(self, monkeypatch: pytest.MonkeyPatch):
        """服务端错误（如超限 400）如实上抛，由检索层降级 RRF。"""
        import httpx

        provider = _make_provider()

        async def fake_post(self: Any, url: str, json: dict[str, Any] | None = None, **kw: Any):
            raise httpx.HTTPStatusError(
                "400", request=httpx.Request("POST", url), response=httpx.Response(400)
            )

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

        with pytest.raises(httpx.HTTPStatusError):
            await provider.rerank("查询", ["文档"])
