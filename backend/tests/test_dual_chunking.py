"""双粒度切分（父子块 P1）单测。

覆盖：token 预算、父子挂载、偏移单调、旧路径不受影响、
chonkie 缺失兜底、超长单元硬切。
"""

from __future__ import annotations

import pytest

from app.services import chunker as ck
from app.services.chunker import (
    chunk_document,
    chunk_document_dual,
    get_default_tokenizer,
    mount_parents,
)

# 公文样例：含章节标题、条款、长句——覆盖 section/paragraph 策略与超长单元
SAMPLE = """第一章 总则
第一条 为了加强安全生产责任追究，防止和减少生产安全事故，根据《中华人民共和国安全生产法》等法律、行政法规，结合本省实际，制定本办法。
第二条 本办法所称安全生产责任追究，是指对未履行或者未正确履行安全生产管理职责的行政机关、生产经营单位及其工作人员，依法追究责任。
第三条 安全生产责任追究坚持实事求是、错责相当、惩教结合的原则。

第二章 责任划分
第四条 各级人民政府及其有关部门应当履行下列安全生产管理职责：（一）研究制定安全生产规划；（二）组织实施安全生产监督检查；（三）组织应急救援演练。
第五条 生产经营单位的主要负责人是本单位的安全生产第一责任人，对本单位的安全生产工作全面负责。"""

PREFIX = "安全生产责任追究办法｜省人民政府｜2026-01-15"


@pytest.fixture(autouse=True)
def _restore_tokenizer():
    """每个用例后恢复 tokenizer 单例（兜底用例会临时置 None）。"""
    yield
    ck._TOKENIZER_TRIED = False
    ck._TOKENIZER = None


def _tokens(text: str) -> int:
    """用 chonkie 复算 token 数（测试内的独立度量，不依赖被测代码路径）。"""
    tok = get_default_tokenizer()
    if tok is None:
        pytest.skip("chonkie 不可用")
    return tok.count_tokens(text)


class TestDualChunking:
    def test_small_within_token_budget(self):
        """小块 token 数不得超过预算（前缀也计入，容 +2 边界）。"""
        small, _ = chunk_document_dual(SAMPLE, PREFIX, strategy="section", small_max_tokens=60)
        assert small, "应产出小块"
        for s in small:
            assert _tokens(s.content) <= 62, f"小块超预算: {s.content[:30]!r}"

    def test_large_within_token_budget(self):
        small, large = chunk_document_dual(SAMPLE, PREFIX, strategy="section", small_max_tokens=60, large_max_tokens=200)
        assert large, "应产出大块"
        for lg in large:
            assert _tokens(lg.content) <= 202, f"大块超预算: {lg.content[:30]!r}"

    def test_granularity_labels_and_prefix(self):
        small, large = chunk_document_dual(SAMPLE, PREFIX, strategy="section")
        assert all(s.granularity == "small" for s in small)
        assert all(lg.granularity == "large" for lg in large)
        # 每个切片都带文档级上下文前缀（检索质量核心机制保留）
        assert all(s.content.startswith(PREFIX) for s in small)
        assert all(lg.content.startswith(PREFIX) for lg in large)

    def test_offsets_within_text_and_monotonic(self):
        small, large = chunk_document_dual(SAMPLE, PREFIX, strategy="section", small_max_tokens=60)
        for chunks in (small, large):
            for c in chunks:
                assert 0 <= c.start < c.end <= len(SAMPLE), f"偏移越界: [{c.start},{c.end}]"
            starts = [c.start for c in chunks]
            assert starts == sorted(starts), "切片偏移应单调不减"

    def test_mount_parents_full_coverage(self):
        """所有小块都能挂到父块，且父块覆盖小块主体（≥50%）。"""
        small, large = chunk_document_dual(SAMPLE, PREFIX, strategy="section", small_max_tokens=60, large_max_tokens=200)
        mounted = mount_parents(small, large)
        assert mounted == len(small)
        for s in small:
            assert s.parent_index is not None
            lg = large[s.parent_index]
            ov = min(s.end, lg.end) - max(s.start, lg.start)
            assert ov / max(s.end - s.start, 1) >= 0.5

    def test_mount_parents_empty_large(self):
        """无大块（极端配置）时不挂载、不报错。"""
        small, _ = chunk_document_dual(SAMPLE, PREFIX, strategy="section", small_max_tokens=60)
        assert mount_parents(small, []) == 0
        assert all(s.parent_index is None for s in small)

    def test_empty_and_blank_text(self):
        assert chunk_document_dual("", PREFIX) == ([], [])
        assert chunk_document_dual("   \n  ", PREFIX) == ([], [])

    def test_overlong_unit_hard_split(self):
        """无边界超长文本：token 窗口硬切，偏移单调、预算受控。"""
        text = "标点缺失的超长段落" * 200
        small, large = chunk_document_dual(
            text, PREFIX, strategy="fixed", small_max_tokens=50, large_max_tokens=300
        )
        assert len(small) > 1 and len(large) > 1
        for s in small:
            assert _tokens(s.content) <= 52
        starts = [s.start for s in small]
        assert starts == sorted(starts), "硬切偏移应单调不减"
        mount_parents(small, large)
        assert all(s.parent_index is not None for s in small)

    def test_fallback_without_chonkie(self):
        """chonkie 不可用：退化为字符计数，切分仍可用（不抛异常）。"""
        ck._TOKENIZER = None
        ck._TOKENIZER_TRIED = True
        small, large = chunk_document_dual(
            "标点缺失的超长段落" * 200, PREFIX, strategy="fixed",
            small_max_tokens=50, large_max_tokens=300,
        )
        assert len(small) > 1 and len(large) > 1
        starts = [s.start for s in small]
        assert starts == sorted(starts)
        mount_parents(small, large)
        assert all(s.parent_index is not None for s in small)

    def test_tiny_document_single_chunks(self):
        """极短文档：小块=大块=全文，挂载自覆盖。"""
        small, large = chunk_document_dual("特此通知。", PREFIX, strategy="paragraph")
        assert len(small) == 1 and len(large) == 1
        mount_parents(small, large)
        assert small[0].parent_index == 0

    def test_legacy_path_unchanged(self):
        """旧单粒度路径（dual_granularity_enabled=false 时使用）行为不变。"""
        legacy = chunk_document(SAMPLE, PREFIX, strategy="section", max_chars=800, overlap=80)
        assert legacy and all(c.content.startswith(PREFIX) for c in legacy)
        assert all(len(c.content) <= 800 + len(PREFIX) + 2 for c in legacy)
