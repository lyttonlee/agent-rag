"""ofdkit 库测试：导航 / 阅读顺序 / 噪音剔除 / 规范化 / 扫描件判定。"""

from __future__ import annotations

from ofdkit.normalize import (
    FONT_GLYPH_FIXES,
    collapse_char_doubling,
    fix_font_glyphs,
    normalize_text,
)

from ofdkit import extract_pages, extract_text, make_ofd

# 公文样例行：标题行 + 正文行 + 页脚页码
PAGE_1 = [
    (20.0, 25.0, "四川省人民政府"),
    (20.0, 40.0, "关于加快提升农业社会化服务水平"),
    (20.0, 55.0, "发展适度规模经营的指导意见"),
    (20.0, 80.0, "各市(州)人民政府ꎬ省政府各部门、各直属机构:"),
    (20.0, 100.0, "现将有关事项通知如下ꎮ"),
    (95.0, 283.0, "—"),
    (100.0, 283.0, "１"),
    (105.0, 283.0, "—"),
]
PAGE_2 = [
    (20.0, 25.0, "一、总体要求"),
    (20.0, 45.0, "聚焦建设新时代更高水平天府粮仓ꎬ紧扣强主体、夯基础、优服务"),
    (20.0, 60.0, "促发展工作思路ꎮ"),
    (95.0, 283.0, "—"),
    (100.0, 283.0, "２"),
    (105.0, 283.0, "—"),
]


class TestExtractBasic:
    def test_basic_extraction(self):
        """基本提取：标题 + 正文 + 页码剔除。"""
        result = extract_text(make_ofd([PAGE_1, PAGE_2]))
        assert result.pages == 2
        assert "四川省人民政府" in result.text
        assert "各市(州)人民政府，省政府各部门、各直属机构:" in result.text  # 乱码已修复
        assert "现将有关事项通知如下。" in result.text
        # 页码被剔除（合并行 "—１—" 命中强页码模式）
        assert "—１—" not in result.text
        assert len(result.removed_lines) >= 2

    def test_no_clean_keeps_noise(self):
        """关闭清洗：页码原文保留（对照审计用；全角数字已被规范化转半角）。"""
        result = extract_text(make_ofd([PAGE_1]), clean_noise=False)
        assert "—1—" in result.text

    def test_reading_order_from_shuffled_runs(self):
        """文档顺序乱序 → 按坐标重排为阅读顺序。"""
        shuffled = list(reversed(PAGE_1))
        result = extract_text(make_ofd([shuffled]))
        lines = result.text.splitlines()
        assert lines[0] == "四川省人民政府"
        assert "关于加快提升农业社会化服务水平" in lines[1]

    def test_same_y_runs_merge_in_x_order(self):
        """同行片段按 X 排序拼接（中文直接相接，不插空格）。"""
        page = [(50.0, 100.0, "人民政府"), (10.0, 100.0, "四川省")]
        result = extract_text(make_ofd([page]))
        assert result.text.strip() == "四川省人民政府"

    def test_local_coords_are_relative_to_boundary(self):
        """TextCode X/Y 是**相对** Boundary 的局部偏移，必须叠加后才参与排序。

        回归背景（2026-10-15 实测）：真实公文的 TextCode 普遍写 X="0.0" Y="6.0"
        （相对绘制原点），旧实现把它当页面绝对坐标，导致所有片段堆到页顶、
        阅读顺序重排失效——细粒度片段文件正文完全乱序。
        本用例固定 Boundary 的绝对位置，仅让局部偏移非零，验证排序仍由
        Boundary 主导（偏移不改变行间相对次序）。
        """
        # 三个片段 Boundary 的绝对 Y 分别 30/60/90；局部偏移统一 (0, 6)
        page = [
            (10.0, 90.0, "第三行"),
            (10.0, 30.0, "第一行"),
            (10.0, 60.0, "第二行"),
        ]
        result = extract_text(make_ofd([page], local_offset=(0.0, 6.0)))
        lines = [ln for ln in result.text.splitlines() if ln.strip()]
        assert lines == ["第一行", "第二行", "第三行"], lines

    def test_local_offset_does_not_reorder(self):
        """同一局部偏移下， Boundary 绝对坐标决定次序（偏移只做平移）。"""
        page = [(10.0, 30.0, "甲"), (10.0, 60.0, "乙"), (10.0, 90.0, "丙")]
        base = [ln for ln in extract_text(make_ofd([page])).text.splitlines() if ln.strip()]
        shifted = [
            ln
            for ln in extract_text(make_ofd([page], local_offset=(3.0, 5.0))).text.splitlines()
            if ln.strip()
        ]
        assert base == shifted == ["甲", "乙", "丙"]

    def test_page_join(self):
        """多页用换行拼接，页序号连续。"""
        result = extract_text(make_ofd([PAGE_1, PAGE_2]))
        assert "一、总体要求" in result.text
        pages = extract_pages(make_ofd([PAGE_1, PAGE_2]))
        assert [p.index for p in pages] == [0, 1]


class TestNavigationVariants:
    def test_no_entry_fallback(self):
        """无 OFD.xml 入口 → 按文件名兜底找 Document.xml。"""
        result = extract_text(make_ofd([PAGE_1], with_entry=False))
        assert "四川省人民政府" in result.text

    def test_no_namespace(self):
        """无命名空间变体（本地名匹配）。"""
        result = extract_text(make_ofd([PAGE_1], no_namespace=True))
        assert "四川省人民政府" in result.text

    def test_no_coords_fallback_to_boundary(self):
        """TextCode 无坐标 → 回退 TextObject Boundary 左上角。"""
        result = extract_text(make_ofd([PAGE_1], with_coords=False))
        assert "四川省人民政府" in result.text
        assert "关于加快提升农业社会化服务水平" in result.text

    def test_empty_package(self):
        """空页列表 → 扫描件结果，不抛异常。"""
        result = extract_text(make_ofd([]))
        assert result.pages == 0
        assert result.is_scanned is True


class TestNoiseFilter:
    def test_page_number_removed(self):
        """页码（含破折线包裹的全角页码）剔除。"""
        result = extract_text(make_ofd([PAGE_1]))
        assert not any(ln.strip().strip("—–-") in ("１", "1", "2") for ln in result.text.splitlines())

    def test_repeated_header_removed(self):
        """跨页高频重复的页眉行剔除（≥50% 页同文本同区域）。"""
        header = (20.0, 15.0, "四川省人民政府办公厅文件")
        pages = [[header, (20.0, 100.0, f"第{i}页正文内容")] for i in range(1, 5)]
        result = extract_text(make_ofd(pages))
        assert "四川省人民政府办公厅文件" not in result.text
        assert "第1页正文内容" in result.text

    def test_repeated_body_line_kept(self):
        """正文区（非页眉脚区）的跨页重复行不删——防误删正文。"""
        line = (20.0, 150.0, "坚持政府引导与市场主导相结合")  # y=150 在正文区
        pages = [[line, (20.0, 200.0, f"第{i}页其他内容")] for i in range(1, 5)]
        result = extract_text(make_ofd(pages))
        assert result.text.count("坚持政府引导与市场主导相结合") == 4

    def test_single_page_no_repeat_removal(self):
        """单页文档不做跨页判重（无统计意义）。"""
        result = extract_text(make_ofd([[(20.0, 100.0, "坚持政府引导与市场主导相结合的原则")]]))
        assert "坚持政府引导与市场主导相结合的原则" in result.text


class TestScannedDetection:
    def test_scanned_page_counted(self):
        """无文字层页计入 scanned_pages（1/4 页未达半数，不算整体扫描件）。"""
        empty_page = [(20.0, 283.0, "—"), (100.0, 283.0, "３"), (105.0, 283.0, "—")]
        pages = [PAGE_1, PAGE_2, [(20.0, 100.0, "过渡页正文内容足够长")], empty_page]
        result = extract_text(make_ofd(pages))
        assert result.scanned_pages == 1
        assert result.is_scanned is False

    def test_all_scanned(self):
        """全部无文字层 → is_scanned=True。"""
        result = extract_text(make_ofd([[(20.0, 100.0, "")]]))
        assert result.is_scanned is True


class TestNormalize:
    def test_glyph_fix_table(self):
        """映射表逐项验证（实测上下文核对过的；码点>0xFFFF 用 8 位 \\U 形式）。"""
        assert FONT_GLYPH_FIXES["\U0000a3ac"] == "，"
        assert FONT_GLYPH_FIXES["\U0000a3ae"] == "。"
        assert FONT_GLYPH_FIXES["\U0000a3bb"] == "；"
        assert FONT_GLYPH_FIXES["\U001001b0"] == "."

    def test_fix_and_unmapped_report(self):
        """命中表修复；未命中可疑区段保留并上报。"""
        fixed, unmapped = fix_font_glyphs("你好ꎮ世界\uf8ff结束")
        assert fixed == "你好。世界\uf8ff结束"
        assert unmapped == ["\uf8ff"]

    def test_halfwidth_alnum(self):
        """全角数字/字母半角化；中文标点不动。"""
        assert normalize_text("２０２５年３月，PM２.５") == "2025年3月，PM2.5"

    def test_zero_width_removed(self):
        """零宽字符剔除。"""
        assert normalize_text("公\u200b文") == "公文"

    def test_doubled_header_collapsed(self):
        """逐字双写行（排版伪粗体页眉）压缩为单份。

        实测：国家发改委 OFD 把「国家发展改革委」写成
        「国国家家发发展展和和改改革革委委员员会会」——不压缩会伤害检索。
        """
        assert collapse_char_doubling("国国家家发发展展和和改改革革委委员员会会") == "国家发展和改革委员会"
        assert collapse_char_doubling("国国家家发发展展和和改改革革委委员员会会发发布布") == "国家发展和改革委员会发布"

    def test_doubling_keeps_normal_reduplication(self):
        """正常叠词/局部重复不被误压缩（判据是整行 80% 成对）。"""
        assert collapse_char_doubling("这个事情需要研究研究") == "这个事情需要研究研究"
        assert collapse_char_doubling("哈哈哈真有意思") == "哈哈哈真有意思"
        assert collapse_char_doubling("2026年") == "2026年"

    def test_doubling_idempotent_in_normalize(self):
        """normalize_text 链路内含双写压缩，且幂等。"""
        once = normalize_text("国国家家发发展展和和改改革革委委员员会会行行政政规规范范性性文文件件")
        assert once == "国家发展和改革委员会行政规范性文件"
        assert normalize_text(once) == once

    def test_idempotent(self):
        """幂等：重复规范化结果不变。"""
        once = normalize_text("各市(州)人民政府ꎬ省政府各部门:")
        assert normalize_text(once) == once
