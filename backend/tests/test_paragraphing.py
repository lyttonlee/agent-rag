"""段落重建单测：软换行合并、标题分段、括号跨行、冒号边界、满行信号。

测试样本形态全部来自真实语料（30 篇政府公报 PDF 提取后的物理行序列）。
"""

from __future__ import annotations

from app.services.paragraphing import (
    looks_like_heading,
    rebuild_paragraphs,
)


class TestHeadingDetection:
    def test_heading_patterns(self):
        """条款/章节标题模式（全角+半角括号都要覆盖——PDF 常转半角）。"""
        assert looks_like_heading("一、总体要求")
        assert looks_like_heading("(一)发挥专业服务公司引领作用。")
        assert looks_like_heading("（二）提升服务质效")
        assert looks_like_heading("第三章 责任划分")
        assert looks_like_heading("第一条 为了加强安全生产")
        assert looks_like_heading("(1) 总则")
        assert looks_like_heading("附件：任务分工表")

    def test_non_headings(self):
        """正文行不误判为标题。"""
        assert not looks_like_heading("2025年全省GDP增长5.5%，超额完成目标")
        assert not looks_like_heading("推进改革。各地区各部门要")
        assert not looks_like_heading("1" * 60)  # 超长数字串


class TestParagraphRebuild:
    def test_soft_wrap_merge(self):
        """PDF 软换行：满行 + 续行合并为一段（段中不拆）。"""
        text = (
            "各市(州)、县(市、区)人民政府，省政府各部门、各直属机构，有关\n"
            "单位:为加快健全我省便捷高效的农业社会化服务体系，经省政府\n"
            "同意，现提出如下指导意见。"
        )
        paras = rebuild_paragraphs(text)
        assert len(paras) == 1
        assert paras[0].text == (
            "各市(州)、县(市、区)人民政府，省政府各部门、各直属机构，有关"
            "单位:为加快健全我省便捷高效的农业社会化服务体系，经省政府"
            "同意，现提出如下指导意见。"
        )

    def test_full_line_with_period_still_merges(self):
        """满行即使以句号结尾也续段（软换行点可能恰落在句号后）。"""
        text = (
            "聚焦建设新时代更高水平天府粮仓，紧扣强主体夯基础优服务促发\n"
            "展工作思路，以提高农业社会化服务质效为目标。以提升全产业链\n"
            "条服务能力为重点，坚持政府引导与市场主导相结合。"
        )
        paras = rebuild_paragraphs(text)
        assert len(paras) == 1

    def test_short_line_with_period_starts_new_paragraph(self):
        """短行 + 句末标点 → 段落结束（落款/特此通知场景）。"""
        text = "现将有关事项通知如下。\n特此通知。"
        paras = rebuild_paragraphs(text)
        assert [p.text for p in paras] == ["现将有关事项通知如下。", "特此通知。"]

    def test_blank_line_is_boundary(self):
        """空行分段（DOCX/TXT 显式分段）。"""
        text = "第一段内容。\n\n第二段内容。"
        paras = rebuild_paragraphs(text)
        assert [p.text for p in paras] == ["第一段内容。", "第二段内容。"]

    def test_heading_forces_new_paragraph(self):
        """标题行强制新段，且标题独立成段。"""
        text = (
            "同意，现提出如下指导意见。\n"
            "一、工作要求\n"
            "以习近平新时代中国特色社会主义思想为指导，全面贯彻党的二十大"
        )
        paras = rebuild_paragraphs(text)
        assert len(paras) == 3
        assert paras[1].text == "一、工作要求"

    def test_doc_no_line_standalone(self):
        """发文字号行独立成段，且不与后行合并。"""
        text = "四川省人民政府办公厅\n川办发〔2025〕36号\n各市(州)人民政府:"
        paras = rebuild_paragraphs(text)
        assert paras[1].text == "川办发〔2025〕36号"

    def test_date_line_standalone(self):
        """落款日期行独立成段。"""
        text = "特此通知。\n四川省人民政府\n2025年10月30日"
        paras = rebuild_paragraphs(text)
        assert paras[-1].text == "2025年10月30日"

    def test_colon_strong_boundary(self):
        """冒号结尾（主送机关）是强边界：主送独立成段，其后正文新起。

        样本复刻真实语料形态（003 川办发36号）：短行标题 + 文号行 +
        冒号结尾的主送行 + 正文。
        """
        text = (
            "四川省人民政府办公厅\n"
            "关于加快提升农业社会化服务水平\n"
            "发展适度规模经营的指导意见\n"
            "川办发〔2025〕36号\n"
            "各市(州)人民政府，省政府各部门、各直属机构，有关单位:\n"
            "为加快健全我省便捷高效的农业社会化服务体系，经省政府同意，现提出如下指导意见。"
        )
        paras = rebuild_paragraphs(text)
        assert len(paras) == 4
        assert paras[0].text.startswith("四川省人民政府办公厅")
        assert paras[1].text == "川办发〔2025〕36号"
        assert paras[2].text.endswith(":")
        assert paras[3].text.startswith("为加快健全")

    def test_bracket_cross_line_merge(self):
        """括号/引号跨行必须合并（不能把 `（` 和 `）` 拆到两段）。"""
        text = "现将有关事项通知如下\n（此件公开发布）"
        paras = rebuild_paragraphs(text)
        assert len(paras) == 1

    def test_quote_open_at_line_end_merges(self):
        """上行以开引号结尾 → 必然未完，续段。"""
        text = "会议指出：“\n做好以下重点工作。"
        paras = rebuild_paragraphs(text)
        assert len(paras) == 1

    def test_latin_word_break_gets_space(self):
        """半角单词跨行断裂补空格，中文直接相接。"""
        text = "详见四川省社科联主页(http://www.scskl.cn)登录系统办理相关业务手续流程说明文档内容"
        paras = rebuild_paragraphs(text)
        assert "http://www.scskl.cn" in paras[0].text

    def test_offsets_cover_original_text(self):
        """段落偏移正确：text[start:end] 还原段落内容（忽略行间空白）。"""
        text = "第一段。\n第二段第一行\n第二段第二行内容足够长以超过满行阈值。"
        paras = rebuild_paragraphs(text)
        for p in paras:
            assert text[p.start : p.end].strip() != ""
        assert paras[0].start == 0
        assert paras[-1].end == len(text)

    def test_disabled_rebuild_only_blank_lines(self):
        """rebuild 关闭：仅按空行分段（保守模式）。"""
        text = "第一行软换行\n第二行软换行\n\n第二段。"
        paras = rebuild_paragraphs(text, enabled=False)
        assert len(paras) == 2

    def test_empty_text(self):
        assert rebuild_paragraphs("") == []
        assert rebuild_paragraphs("   \n\n  ") == []
