"""公文版式 DOCX 渲染器单测（2026-10-15 用户需求：导出不能保留 md 语法）。

验证渲染器把 Markdown 转为**真实 Word 样式**而非纯文本：
标题层级字体（黑体/楷体/仿宋）、正文首行缩进 2 字符、固定行距、
两端对齐、表格全框线、行内加粗、落款右对齐、红头、页脚页码域、
中文软换行拼接、题名去重。

运行：uv run pytest tests/test_docx_render.py -v
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest
from docx import Document as DocxDocument
from docx.oxml.ns import qn

from app.services.docx_render import (
    BODY_FONT,
    HEADING1_FONT,
    HEADING2_FONT,
    TITLE_FONT,
    _join_soft_lines,
    _parse_blocks,
    render_docx,
)

# 典型公文草稿（LLM 常见输出形态：md 标题 + 加粗 + 列表 + 表格 + 落款）
SAMPLE_MD = """## 关于加快提升农业社会化服务水平发展适度规模经营的指导意见

各有关单位：

为进一步提升农业社会化服务水平，现就有关事项**通知**如下：

### 一、总体要求

以习近平新时代中国特色社会主义思想为指导，立足农业农村发展需求。

- 坚持市场主导
- 坚持政府引导

### 二、重点任务

| 序号 | 任务 | 责任单位 |
|---|---|---|
| 1 | 建设区域农机服务中心 | 农业农村厅 |
| 2 | 培育社会化服务组织 | 各市（州） |

本通知自印发之日起施行。

XX省农业农村厅
2026年10月15日
"""


def _doc(blob: bytes) -> DocxDocument:
    return DocxDocument(io.BytesIO(blob))


def _xml_part(blob: bytes, part: str) -> str:
    """取 docx 内部 XML（document.xml / footer*.xml 等）。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read(part).decode("utf-8")


def _all_text(doc: DocxDocument) -> str:
    """全文文本（段落 + 表格单元格——表格内容不在 doc.paragraphs 里）。"""
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _eastasia_of(run) -> str | None:
    rpr = run._element.rPr
    if rpr is None or rpr.rFonts is None:
        return None
    return rpr.rFonts.get(qn("w:eastAsia"))


# ---- 块解析（纯函数） ----


def test_parse_blocks_structure():
    """标题/列表/表格/落款均被识别为对应块类型。"""
    blocks = _parse_blocks(SAMPLE_MD)
    kinds = [b[0] for b in blocks]
    assert "heading" in kinds
    assert "ul" in kinds
    assert "table" in kinds
    # 表格两行数据（表头 + 2 行）
    table = next(b for b in blocks if b[0] == "table")
    assert len(table[1]) == 3
    assert table[1][0] == ["序号", "任务", "责任单位"]


def test_join_soft_lines_cjk_and_latin():
    """中文软换行直接相接；西文之间补空格。"""
    assert _join_soft_lines(["关于加快提升", "农业服务水平"]) == "关于加快提升农业服务水平"
    assert _join_soft_lines(["hello", "world"]) == "hello world"


# ---- 渲染结果 ----


def test_no_markdown_syntax_left():
    """红线：正文不得残留 ## / ** / - 等 Markdown 语法。"""
    blob = render_docx(title="测试通知", content=SAMPLE_MD)
    text = _all_text(_doc(blob))
    assert "##" not in text
    assert "**" not in text
    assert not re.search(r"(?m)^- ", text)
    # 语义内容完整保留
    assert "农业社会化服务水平" in text
    assert "各市（州）" in text


def test_heading_fonts_by_level():
    """标题层级映射公文字体：##→黑体，###→楷体。"""
    blob = render_docx(title="测试通知", content=SAMPLE_MD)
    doc = _doc(blob)
    h2 = next(p for p in doc.paragraphs if p.text.startswith("关于加快提升"))
    h3 = next(p for p in doc.paragraphs if p.text.startswith("一、总体要求"))
    assert _eastasia_of(h2.runs[0]) == HEADING1_FONT  # 黑体
    assert _eastasia_of(h3.runs[0]) == HEADING2_FONT  # 楷体


def test_title_rendered_and_deduped():
    """题名渲染为居中标题；正文首个同名 H1 不重复渲染。"""
    blob = render_docx(title="关于加快提升农业社会化服务水平发展适度规模经营的指导意见",
                       content=SAMPLE_MD)
    doc = _doc(blob)
    title_para = doc.paragraphs[0]
    assert title_para.text == "关于加快提升农业社会化服务水平发展适度规模经营的指导意见"
    assert _eastasia_of(title_para.runs[0]) == TITLE_FONT
    assert title_para.alignment is not None  # 居中
    # 同名 H1 只出现一次（标题）+ 一次（正文表格外的 H1 跳过）
    hits = [p for p in doc.paragraphs if p.text.startswith("关于加快提升")]
    assert len(hits) == 1


def test_body_paragraph_indent_and_spacing():
    """正文段：首行缩进 2 字符（firstLineChars=200）+ 固定行距 28 磅。

    注意：样本不能以冒号结尾——那会命中"主送机关顶格"规则（首段且
    ≤40 字）而不缩进，那是公文规范行为（见 test_addressee_top_aligned）。
    """
    blob = render_docx(title="测试", content="为进一步提升农业社会化服务水平，现就有关事项通知如下。")
    doc = _doc(blob)
    p = doc.paragraphs[-1]
    ind = p._p.pPr.find(qn("w:ind"))
    assert ind is not None
    assert ind.get(qn("w:firstLineChars")) == "200"
    assert p.paragraph_format.line_spacing.pt == 28


def test_addressee_top_aligned():
    """主送机关（首段 + 冒号结尾）顶格：不产生首行缩进。"""
    blob = render_docx(title="测试", content="各有关单位：\n\n现就有关事项通知如下。")
    doc = _doc(blob)
    first = doc.paragraphs[1]  # [0] 是标题
    assert first.text == "各有关单位："
    assert first._p.pPr.find(qn("w:ind")) is None
    second = doc.paragraphs[2]
    assert second._p.pPr.find(qn("w:ind")) is not None


def test_inline_bold_and_table():
    """行内加粗生成独立 bold run；表格渲染为 w:tbl 且带全框线样式。"""
    blob = render_docx(title="测试", content="现就**有关事项**通知如下。")
    doc = _doc(blob)
    p = doc.paragraphs[-1]
    bold_runs = [r for r in p.runs if r.font.bold]
    assert bold_runs and bold_runs[0].text == "有关事项"
    assert _eastasia_of(bold_runs[0]) == BODY_FONT

    # 表格检查要用含表格的正文（上面这句没有表格）
    blob2 = render_docx(title="测试", content=SAMPLE_MD)
    xml = _xml_part(blob2, "word/document.xml")
    assert "<w:tbl>" in xml
    assert "TableGrid" in xml  # Table Grid 全框线样式
    assert "农业农村厅" in _all_text(_doc(blob2))  # 表头数据在单元格里


def test_signature_right_aligned():
    """落款（机关名 + 日期）右对齐，其余正文保持两端对齐。"""
    blob = render_docx(title="测试", content=SAMPLE_MD)
    doc = _doc(blob)
    date_para = next(p for p in doc.paragraphs if "2026年10月15日" in p.text)
    org_para = next(p for p in doc.paragraphs if p.text.strip() == "XX省农业农村厅")
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    assert date_para.alignment == WD_ALIGN_PARAGRAPH.RIGHT
    assert org_para.alignment == WD_ALIGN_PARAGRAPH.RIGHT


def test_red_head_title_color():
    """红头模板：标题描红。"""
    blob = render_docx(title="红头文件", content="正文", layout={"red_head": True})
    doc = _doc(blob)
    title_run = doc.paragraphs[0].runs[0]
    assert title_run.font.color.rgb is not None
    assert str(title_run.font.color.rgb) == "FF0000"


def test_layout_font_override():
    """模板 layout.font 覆盖正文字体。"""
    blob = render_docx(title="测试", content="正文一段", layout={"font": "仿宋"})
    doc = _doc(blob)
    assert _eastasia_of(doc.paragraphs[-1].runs[0]) == "仿宋"


def test_footer_page_number_field():
    """页脚含 PAGE 域（打开时动态渲染页码）。"""
    blob = render_docx(title="测试", content="正文")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        footers = [n for n in zf.namelist() if re.match(r"word/footer\d+\.xml", n)]
        assert footers, "应生成页脚部件"
        xml = zf.read(footers[0]).decode("utf-8")
    assert "PAGE" in xml  # fldSimple 域指令


def test_citations_and_sensitive_note():
    """参考资料区与脱敏说明按公文样式附于文末。"""
    blob = render_docx(
        title="测试",
        content="正文",
        citations=[{"title": "某通知", "doc_no": "X发〔2026〕1号"}],
        sensitive_hits=["机密词"],
    )
    text = _all_text(_doc(blob))
    assert "参考资料" in text
    assert "[1] 《某通知》X发〔2026〕1号" in text
    assert "敏感词命中" in text and "机密词" in text


def test_page_setup_a4_margins():
    """A4 版面 + 公文页边距（上 3.7cm）。

    注意用 .cm 浮点近似比较：docx 内部以 twips 存储，cm→twips→EMU
    往返有取整误差（21cm=11906.13twips→11906），精确相等会误报。
    """
    blob = render_docx(title="测试", content="正文")
    sec = _doc(blob).sections[0]
    assert sec.page_width.cm == pytest.approx(21.0, abs=0.01)
    assert sec.page_height.cm == pytest.approx(29.7, abs=0.01)
    assert sec.top_margin.cm == pytest.approx(3.7, abs=0.01)
    assert sec.bottom_margin.cm == pytest.approx(3.5, abs=0.01)
    assert sec.left_margin.cm == pytest.approx(2.8, abs=0.01)
    assert sec.right_margin.cm == pytest.approx(2.6, abs=0.01)
