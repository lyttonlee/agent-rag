"""文本规范化单测：乱码码点映射、全半角、零宽、页码模式、跨页重复剔除。

映射表数据来源：2026-10-15 对 30 篇四川省政府公报 PDF 的全量实测
（上下文逐一人工核对），见 ofdkit/normalize.py 注释。
"""

from __future__ import annotations

import pytest
from ofdkit.noise import Line, filter_noise_lines, is_page_number

from app.services.textnorm import (
    FONT_GLYPH_FIXES,
    fix_font_glyphs,
    normalize_text,
    to_halfwidth_alnum,
)

# 真实语料中的乱码形态（方正书版系 PDF 的 ToUnicode 错位）
PUA_TEXT = "各市(州)人民政府ꎬ省政府各部门:现将有关事项通知如下ꎮ特此通知ꎮ"
GLYPH_FIXED = "各市(州)人民政府，省政府各部门:现将有关事项通知如下。特此通知。"


class TestGlyphFix:
    def test_real_corpus_sentence(self):
        """语料原句修复：逗号/句号/冒号全部归位。"""
        fixed, unmapped = fix_font_glyphs(PUA_TEXT)
        assert fixed == GLYPH_FIXED
        assert unmapped == []

    def test_full_mapping_table(self):
        """映射表六项逐项验证（含 PM2.5 / 间隔号 / 8:2 比例形态）。"""
        cases = {
            "全省PM２ꎮ５平均浓度": "全省PM２。５平均浓度",  # 占位：下面用真实码点
        }
        del cases  # 形态用例统一用真实码点字符，避免复制歧义
        assert FONT_GLYPH_FIXES["\U0000a3ac"] == "，"
        assert FONT_GLYPH_FIXES["\U0000a3ae"] == "。"
        assert FONT_GLYPH_FIXES["\U0000a3bb"] == "；"
        assert FONT_GLYPH_FIXES["\U001001b0"] == "."
        assert FONT_GLYPH_FIXES["\U00100170"] == "·"
        assert FONT_GLYPH_FIXES["\U001003d1"] == ":"
        fixed, _ = fix_font_glyphs("全省ＰＭ２\U001001b0５平均浓度不\U0000a3ae")
        assert fixed == "全省ＰＭ２.５平均浓度不。"

    def test_unmapped_suspect_reported_not_dropped(self):
        """未命中映射表的可疑区段字符：保留原文 + 上报（不静默丢弃）。"""
        fixed, unmapped = fix_font_glyphs("正常\uf8ff文本")
        assert fixed == "正常\uf8ff文本"
        assert unmapped == ["\uf8ff"]

    def test_halfwidth_alnum(self):
        """全角数字/字母半角化；中文标点与全角空格不动。"""
        assert to_halfwidth_alnum("２０２５年３月，PM２.５") == "2025年3月，PM2.5"
        assert to_halfwidth_alnum("　测试（一）") == "　测试（一）"

    def test_zero_width_removed(self):
        """零宽字符剔除。"""
        assert normalize_text("公\u200b文\ufeff库") == "公文库"

    def test_normalize_idempotent(self):
        """幂等：重复执行结果不变。"""
        once = normalize_text(PUA_TEXT + "２０２５年")
        assert normalize_text(once) == once


class TestPageNumberPattern:
    @pytest.mark.parametrize(
        "text,in_zone,expected",
        [
            ("- 1 -", False, True),  # 强模式：任意位置
            ("— １ —", False, True),  # 破折线数字簇（全角数字）
            ("第 3 页", False, True),
            ("第3页/共8页", False, True),
            ("3 / 8", False, True),
            ("1——", False, True),  # 拆行页码的任意顺序簇
            ("１２３", True, True),  # 弱模式：仅页眉/脚区
            ("１２３", False, False),  # 正文区纯数字不删（可能是序号）
            ("42", False, False),
            ("一、总体要求", True, False),  # 正文形态不误判
        ],
    )
    def test_patterns(self, text, in_zone, expected):
        assert is_page_number(text, in_margin_zone=in_zone) is expected


class TestNoiseFilter:
    H = 842.0  # A4 页高（pt）

    def _lines(self, spec: list[tuple[int, float, str]]) -> list[Line]:
        """spec: [(page, y, text)]"""
        return [Line(text=t, page=p, y=y) for p, y, t in spec]

    def test_dash_page_number_removed(self):
        """页脚 `- N -` 剔除（强模式，任意位置）。"""
        lines = self._lines([(0, 400.0, "正文内容"), (0, 800.0, "- 1 -")])
        kept, removed = filter_noise_lines(lines, {0: self.H})
        assert [ln.text for ln in kept] == ["正文内容"]
        assert removed == ["- 1 -"]

    def test_split_page_number_merged_then_removed(self):
        """被拆成三行的页码（`1`/`-`/`-`）在页边缘合并后整体剔除。"""
        lines = self._lines([(0, 400.0, "正文内容"), (0, 798.0, "1"), (0, 800.0, "-"), (0, 800.0, "-")])
        kept, removed = filter_noise_lines(lines, {0: self.H})
        assert [ln.text for ln in kept] == ["正文内容"]
        assert removed == ["1--"]  # 三行合并为一个页码行后整体删除

    def test_repeated_header_removed(self):
        """跨页高频重复的页眉行剔除。"""
        lines = self._lines(
            [(p, 60.0, "四川省人民政府办公厅文件") for p in range(4)]
            + [(p, 300.0, f"第{p + 1}页正文") for p in range(4)]
        )
        kept, removed = filter_noise_lines(lines, {p: self.H for p in range(4)})
        assert all("办公厅文件" not in ln.text for ln in kept)
        assert len(kept) == 4  # 正文全部保留
        assert len(removed) == 4  # 页眉全部剔除

    def test_repeated_body_line_kept(self):
        """正文区（页中）的跨页重复行不删——防误删正文。"""
        lines = self._lines([(p, 400.0, "坚持政府引导与市场主导相结合") for p in range(4)])
        kept, removed = filter_noise_lines(lines, {p: self.H for p in range(4)})
        assert len(kept) == 4 and not removed

    def test_removed_lines_auditable(self):
        """被删行原文保留，供审计"到底删了什么"。"""
        lines = self._lines([(0, 800.0, "- 7 -")])
        _, removed = filter_noise_lines(lines, {0: self.H})
        assert removed == ["- 7 -"]
