"""查询改写（P2）单测：策略路由、解析清洗、去重、多部识别。"""

from __future__ import annotations

import pytest

from app.services import query_rewrite as qr
from app.services.query_rewrite import (
    _looks_multi_part,
    _parse_lines,
    rewrite_queries,
)


@pytest.fixture()
def mock_llm(monkeypatch):
    """按 prompt 类型返回预设输出，记录调用次数。"""
    calls: list[str] = []

    async def _fake(prompt: str) -> str:
        calls.append(prompt)
        if "改写结果" in prompt:
            return '"安全生产责任追究相关规定"'
        if "假想段落" in prompt:
            return "各级人民政府及其有关部门应当履行安全生产管理职责，"
            "对未履行职责的，依法追究相关责任人的责任。"
        if "子问题" in prompt:
            return "1. 安全生产责任追究的依据是什么\n2、责任如何划分\n- 追究程序有哪些"
        return ""

    monkeypatch.setattr(qr, "_llm_rewrite", _fake)
    return calls


class TestParsing:
    def test_parse_lines_strips_numbering(self):
        text = "1. 第一个问题\n2、第二个问题\n③ 第三个问题\n- 第四项\n普通一行"
        assert _parse_lines(text) == ["第一个问题", "第二个问题", "第三个问题", "第四项", "普通一行"]

    def test_parse_lines_empty(self):
        assert _parse_lines("") == []
        assert _parse_lines("\n\n  \n") == []

    def test_multi_part_detection(self):
        assert _looks_multi_part("汛期值班怎么安排；同时后勤如何保障")
        assert _looks_multi_part("安全生产责任追究的依据以及追究程序分别是什么")
        assert not _looks_multi_part("安全生产责任追究")
        assert not _looks_multi_part("短查询？")  # 过短不判多部


class TestStrategies:
    async def test_synonym_strategy(self, mock_llm):
        variants = await rewrite_queries("出事谁负责", strategy="synonym")
        assert variants == ["安全生产责任追究相关规定"]
        assert len(mock_llm) == 1

    async def test_hyde_strategy(self, mock_llm):
        variants = await rewrite_queries("值班表怎么排", strategy="hyde")
        assert len(variants) == 1
        assert "安全生产管理职责" in variants[0]  # 假想段落整体作为 query

    async def test_decompose_strategy(self, mock_llm):
        variants = await rewrite_queries(
            "安全生产责任追究的依据是什么；责任如何划分；追究程序有哪些",
            strategy="decompose",
        )
        assert len(variants) == 3  # 编号/符号行首均被剥离

    async def test_decompose_falls_back_to_synonym(self, monkeypatch):
        """拆解无结果（非多部查询）→ 退化为同义扩展兜底。"""
        async def _empty_decompose(prompt: str) -> str:
            return "" if "子问题" in prompt else '"同义改写"'

        monkeypatch.setattr(qr, "_llm_rewrite", _empty_decompose)
        variants = await rewrite_queries("简单查询", strategy="decompose")
        assert variants == ["同义改写"]

    async def test_auto_single_part(self, mock_llm):
        """auto + 单部查询 → 同义 + 假想双路（顺序：S1 在前）。"""
        variants = await rewrite_queries("出事谁负责", strategy="auto")
        assert len(variants) == 2
        assert variants[0] == "安全生产责任追究相关规定"
        assert "安全生产管理职责" in variants[1]

    async def test_auto_multi_part(self, mock_llm):
        """auto + 多部查询 → 拆解优先，同义兜底。"""
        variants = await rewrite_queries(
            "值班怎么安排；同时后勤如何保障", strategy="auto"
        )
        assert len(variants) == 4  # 3 个子问题 + 1 个同义兜底
        assert variants[0] == "安全生产责任追究的依据是什么"
        assert variants[-1] == "安全生产责任追究相关规定"

    async def test_empty_query(self, mock_llm):
        assert await rewrite_queries("", strategy="auto") == []
        assert await rewrite_queries("   ", strategy="auto") == []
        assert mock_llm == []

    async def test_variants_exclude_original_and_dedupe(self, monkeypatch):
        """改写结果等于原 query / 重复变体都被剔除。"""
        async def _echo(prompt: str) -> str:
            return "同一个查询\n同一个查询\n原query" if "改写结果" in prompt else ""

        monkeypatch.setattr(qr, "_llm_rewrite", _echo)
        variants = await rewrite_queries("原query", strategy="synonym")
        assert variants == ["同一个查询"]

    async def test_unknown_strategy_falls_back_auto(self, mock_llm):
        """非法策略名按 auto 处理（配置白名单失效时的防御）。"""
        variants = await rewrite_queries("出事谁负责", strategy="bogus-strategy")
        assert len(variants) == 2
