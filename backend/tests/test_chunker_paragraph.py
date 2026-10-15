"""段落优先切分单测（V2.0 核心）：段内不切、四级降级、预算、偏移、挂载。

红线：放得下的自然段绝不切断；必须切时依次在句边界 → 子句边界 →
token 硬切处下手，且每片都含完整前缀、不超 token 预算。
"""

from __future__ import annotations

import pytest

from app.services import chunker as ck
from app.services.chunker import (
    _section_units,
    chunk_document,
    chunk_document_dual,
    get_default_tokenizer,
    mount_parents,
)

PREFIX = "安全生产责任追究办法｜省人民政府｜2026-01-15"

# 三个自然段：段1/段2 放得下（合并后不超预算），段3 超预算（触发降级）
PARA_SHORT_1 = "第一条 为了加强安全生产责任追究，防止和减少生产安全事故，制定本办法。"
PARA_SHORT_2 = "第二条 本办法所称安全生产责任追究，是指对未履行安全生产管理职责的行为。"
# 多句长段（每句都是完整句子，测句边界降级）
PARA_LONG = (
    "第三条 安全生产责任追究坚持实事求是、错责相当、惩教结合的原则。"
    "坚持标本兼治、惩前毖后、治病救人的方针。"
    "坚持依规依纪依法、客观公正、审慎稳妥的要求。"
    "坚持权责一致、错责相当的原则。"
    "坚持惩戒与教育相结合、宽严相济的理念。"
    "切实起到教育大多数、惩处极少数的效果。"
    "推动安全生产责任制全面落实到位。"
    "为全省经济社会高质量发展提供坚实保障。"
)
TEXT = f"{PARA_SHORT_1}\n{PARA_SHORT_2}\n{PARA_LONG}"


@pytest.fixture(autouse=True)
def _restore_tokenizer():
    yield
    ck._TOKENIZER_TRIED = False
    ck._TOKENIZER = None


def _tok(texts: list[str]) -> list[int]:
    tok = get_default_tokenizer()
    if tok is None:
        pytest.skip("chonkie 不可用")
    return list(tok.count_tokens_batch(texts))


def _bodies(chunks) -> list[str]:
    """去掉前缀行，返回切片正文（按 \\n 拆出的单元列表展平）。"""
    return [c.content.split("\n", 1)[1] if "\n" in c.content else "" for c in chunks]


class TestParagraphFirst:
    def test_paragraph_units_not_cut(self):
        """放得下的自然段整段入块，不被切断。"""
        smalls, _ = chunk_document_dual(
            PARA_SHORT_1 + "\n" + PARA_SHORT_2,
            PREFIX,
            small_max_tokens=300,
            small_overlap_tokens=40,
        )
        # 两段合并进同一块（总 token 不超预算），且各自完整
        body = _bodies(smalls)[0]
        assert PARA_SHORT_1 in body
        assert PARA_SHORT_2 in body
        assert len(smalls) == 1

    def test_long_paragraph_split_at_sentence_boundary(self):
        """超预算段按句边界降级：每片以句号结尾，无半句话腰斩。"""
        smalls, _ = chunk_document_dual(PARA_LONG, PREFIX, small_max_tokens=120, small_overlap_tokens=0)
        assert len(smalls) >= 2
        for body in _bodies(smalls):
            assert body.rstrip().endswith("。"), f"断点不在句边界: {body[-20:]!r}"

    def test_single_sentence_over_budget_falls_to_clause(self):
        """单句超预算 → 子句边界（逗号）降级，不断子句。"""
        # 一个超长无句号句子：只有逗号边界（末尾带句号收尾）
        long_sentence = "，".join(["各地区各部门要高度重视安全生产责任追究工作"] * 12) + "。"
        smalls, _ = chunk_document_dual(long_sentence, PREFIX, small_max_tokens=80, small_overlap_tokens=0)
        assert len(smalls) >= 2
        for body in _bodies(smalls):
            assert body.rstrip().endswith(("，", "。")), f"断点不在子句边界: {body[-20:]!r}"

    def test_no_boundary_hard_split_within_budget(self):
        """无语义边界（纯数字串）→ token 硬切兜底，仍不超预算。"""
        digits = "9" * 500
        smalls, _ = chunk_document_dual(digits, PREFIX, small_max_tokens=100, small_overlap_tokens=0)
        counts = _tok([s.content for s in smalls])
        assert all(c <= 102 for c in counts)  # +2 容差

    def test_budget_includes_prefix(self):
        """token 预算覆盖完整切片（前缀 + 正文）。"""
        smalls, _ = chunk_document_dual(TEXT, PREFIX, small_max_tokens=150, small_overlap_tokens=20)
        counts = _tok([s.content for s in smalls])
        assert all(c <= 152 for c in counts)

    def test_every_chunk_carries_prefix(self):
        """每片都带文档级上下文前缀。"""
        smalls, larges = chunk_document_dual(TEXT, PREFIX)
        for c in smalls + larges:
            assert c.content.startswith(PREFIX)
            assert c.prefix == PREFIX

    def test_offsets_monotonic_and_within_text(self):
        """偏移严格单调且落在正文范围内。"""
        smalls, _ = chunk_document_dual(TEXT, PREFIX, small_max_tokens=100)
        for i in range(len(smalls) - 1):
            assert smalls[i].start < smalls[i + 1].start
            assert smalls[i].end <= smalls[i + 1].end
        for s in smalls:
            assert 0 <= s.start < s.end <= len(TEXT)

    def test_parent_mounting(self):
        """父子挂载：每个小块都有父块。"""
        smalls, larges = chunk_document_dual(TEXT, PREFIX, small_max_tokens=80)
        assert mount_parents(smalls, larges) == len(smalls)
        assert all(s.parent_index is not None for s in smalls)

    def test_chonkie_missing_fallback(self):
        """chonkie 缺失 → 字符近似计数，切分不崩、不超字符预算。"""
        ck._TOKENIZER_TRIED = True
        ck._TOKENIZER = None
        smalls, _ = chunk_document_dual(TEXT, PREFIX, small_max_tokens=60, small_overlap_tokens=0)
        assert smalls
        for c in smalls:
            assert len(c.content) <= 62  # 字符近似：1 字≈1 token，+2 容差


class TestStrategies:
    def test_sentence_strategy(self):
        """sentence 策略：句单元，单句完整。"""
        pieces = chunk_document(PARA_SHORT_1 + PARA_SHORT_2, PREFIX, strategy="sentence", max_chars=40)
        assert all(p.content.rstrip().endswith("。") for p in pieces)

    def test_section_strategy(self):
        """section 策略：条款标题 + 其下正文作为整体单元。"""
        text = "一、总体要求\n以习近平新时代中国特色社会主义思想为指导。\n二、重点任务\n落实各项部署。"
        units = _section_units(text)
        assert units == [
            "一、总体要求\n以习近平新时代中国特色社会主义思想为指导。",
            "二、重点任务\n落实各项部署。",
        ]
        # 切片不丢内容：全部单元按序落入切片正文
        pieces = chunk_document(text, PREFIX, strategy="section", max_chars=200)
        body = "\n".join(p.content.split("\n", 1)[1] for p in pieces)
        for u in units:
            assert u in body

    def test_fixed_strategy_ignores_boundaries(self):
        """fixed 策略：定长窗口，无视边界。"""
        pieces = chunk_document(TEXT, PREFIX, strategy="fixed", max_chars=50, overlap=0)
        assert all(len(p.content.split("\n", 1)[1]) <= 50 for p in pieces)

    def test_default_strategy_is_paragraph(self):
        """默认策略 paragraph：自然段单元。"""
        smalls, _ = chunk_document_dual(PARA_SHORT_1 + "\n" + PARA_SHORT_2, PREFIX, small_max_tokens=300)
        assert len(smalls) == 1  # 两段合并，无切断

    def test_rebuild_disabled_degrades_to_blank_lines(self):
        """段落重建关闭：仅按空行分段（已显式分段的文本不受影响）。"""
        docx_like = PARA_SHORT_1 + "\n" + PARA_SHORT_2  # 无空行：退化为单段
        smalls, _ = chunk_document_dual(
            docx_like, PREFIX, small_max_tokens=300, rebuild_paragraphs_flag=False
        )
        assert len(smalls) == 1


class TestLegacySingleGranularity:
    def test_chunk_document_paragraph_units(self):
        """单粒度存量路径：段落单元 + 字符预算。"""
        pieces = chunk_document(PARA_SHORT_1 + "\n" + PARA_SHORT_2, PREFIX, max_chars=800)
        assert len(pieces) == 1
        assert PARA_SHORT_1 in pieces[0].content

    def test_over_long_unit_char_graceful_split(self):
        """单粒度路径的超长段同样走句边界降级。"""
        pieces = chunk_document(PARA_LONG, PREFIX, max_chars=60, overlap=0)
        assert len(pieces) >= 2
        for p in pieces:
            assert p.content.rstrip().endswith("。")
