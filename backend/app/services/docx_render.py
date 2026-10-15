"""Markdown → 公文版式 DOCX 渲染器（导出样式重生，2026-10-15）。

背景（用户反馈）：导出的 docx 把 `##`、`**`、`-` 等 Markdown 语法原样
落成了纯文本——"没有格式"。根因是旧导出只做 `split("\\n")` +
`add_paragraph()`，从不解析 Markdown 结构。

本模块把生成结果（LLM 自然输出的 Markdown）渲染为**符合公文版式**
（参照 GB/T 9704 党政机关公文格式）的真实 Word 样式：

| 元素 | 字体 | 字号 | 版式 |
|---|---|---|---|
| 标题（文档题名） | 方正小标宋简体 | 二号 22pt | 居中，红头可选 |
| 一级标题（## / 一、） | 黑体 | 三号 16pt | 无缩进，段前距 |
| 二级标题（### / （一）） | 楷体_GB2312 | 三号 16pt | 无缩进 |
| 三级标题（####） | 仿宋_GB2312 加粗 | 三号 16pt | 无缩进 |
| 正文 | 仿宋_GB2312 | 三号 16pt | 首行缩进 2 字符，固定行距 28 磅，两端对齐 |
| 列表 | 仿宋_GB2312 | 三号 16pt | 悬挂缩进 |
| 表格 | 表头黑体 / 单元格仿宋 | 五号 10.5pt | 全框线，表头加粗 |
| 页码 | 宋体 | 四号 14pt | 页脚居中 `— N —` |

设计要点：
- **字体经 rFonts w:eastAsia 显式声明**：python-docx 的 `font.name` 只写
  ascii/hAnsi，不写 eastAsia——中文在 Word/WPS 里会掉回宋体。本模块每个
  run 都补 eastAsia，保证中文字体真正生效。
- **首行缩进用 firstLineChars=200**（2 字符）而非固定磅值：Word 的
  "首行缩进 2 字符"语义随字号缩放，换字号不用改代码。
- **版式参数可由模板 layout 覆盖**（font / red_head / title_font …），
  与模板库的 `layout` JSON 对齐；缺项走公文标准默认值。
- **落款右对齐启发式**：末尾「机关名 + 日期」成对出现才右对齐（单见
  日期行也右对齐），避免把正文短句误当落款。
- **主送机关顶格**：仅首个自然段且以冒号结尾（如"各有关单位："）时
  取消首行缩进——公文主送机关顶格是刚性规范。

依赖：python-docx（已在主服务依赖中）；无新增三方依赖。
"""

from __future__ import annotations

import io
import re

from docx import Document as DocxDocument
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# =========================================================================
# 公文版式常量（GB/T 9704 惯例；可被模板 layout 覆盖）
# =========================================================================

#: 正文（三号仿宋）
BODY_FONT = "仿宋_GB2312"
BODY_SIZE = 16
#: 标题（二号方正小标宋）；授权字体缺失时 Word/WPS 自动回退，不影响结构
TITLE_FONT = "方正小标宋简体"
TITLE_SIZE = 22
#: 一级标题（三号黑体）/ 二级标题（三号楷体）
HEADING1_FONT = "黑体"
HEADING2_FONT = "楷体_GB2312"
HEADING_SIZE = 16
#: 表格（五号）
TABLE_FONT = BODY_FONT
TABLE_HEADER_FONT = "黑体"
TABLE_SIZE = 10.5
#: 页码（四号宋体）
PAGE_NUMBER_FONT = "宋体"
PAGE_NUMBER_SIZE = 14
#: 正文固定行距（磅）——公文排版标准值
BODY_LINE_SPACING = 28
#: A4 版面与页边距（上 3.7 / 下 3.5 / 左 2.8 / 右 2.6 cm）
PAGE_MARGINS = (Cm(3.7), Cm(3.5), Cm(2.8), Cm(2.6))
#: 表头底纹（浅灰，打印友好）
TABLE_HEADER_FILL = "F2F2F2"
#: 红头文件标题色
RED_HEAD_COLOR = "FF0000"

# ---- 结构识别正则 ----
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^[-*+·]\s+(.*)$")
_OL_RE = re.compile(r"^(\d{1,2})[.、)]\s*(.*)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_BOLD_PARA_RE = re.compile(r"^\*\*[^*\n]+\*\*$")
#: 落款日期行：2026年10月15日（允许空格）
_SIGN_DATE_RE = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$")
#: 行尾标点（用于"短行≠落款"排除）
_END_PUNCT_RE = re.compile(r"[。！？；…]$")
#: CJK 字符（软换行拼接判定：中文之间不留空格）
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
#: 行内格式：**加粗** / *斜体* / `代码`
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|\*[^*\n]+?\*|`[^`\n]+?`)")

# 块类型：(kind, ...)
#   ("heading", level, text) / ("para", text) / ("para_right", text)
#   ("ul", text) / ("ol", num, text) / ("table", rows) / ("quote", text) / ("hr",)


# =========================================================================
# 版式基础工具
# =========================================================================


def _set_run_font(
    run,
    eastasia: str,
    size: float,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
) -> None:
    """设置 run 字体——**必须显式写 eastAsia**，否则中文掉回默认宋体。

    python-docx 的 `font.name` 只写 w:ascii/w:hAnsi；Word 渲染中文时
    读的是 w:rFonts 的 w:eastAsia 属性。两者都写才是完整方案。
    """
    run.font.name = eastasia          # ascii + hAnsi（西文字符）
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), eastasia)


def _set_first_line_indent_chars(paragraph, chars: int = 200) -> None:
    """首行缩进 N/100 字符（200 = 2 字符，随字号缩放）。

    直接用 `paragraph_format.first_line_indent = Pt(32)` 是固定磅值，
    换字号（如表格内 10.5pt）会出现"缩进 3 字符"的观感；firstLineChars
    是 Word 原生"按字符缩进"语义，公文排版标准做法。
    用 get_or_add_ind() 而非手工 append——OOXML 对 pPr 子元素顺序有
     schema 约束（spacing → ind → jc），乱序会被 Word 判定为文档损坏。
    """
    ppr = paragraph._p.get_or_add_pPr()
    ind = ppr.get_or_add_ind()
    ind.set(qn("w:firstLineChars"), str(chars))
    # 兜底磅值（不识别 Chars 的老查看器用）：16pt × 2 = 32pt = 640 缇
    ind.set(qn("w:firstLine"), "640")


def _new_paragraph(doc, *, line_spacing=None, space_before=0, space_after=0):
    """新建段落并统一设置段前/段后距与行距（公文紧凑排版）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    if line_spacing is not None:
        pf.line_spacing = line_spacing
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    return p


def _setup_page(doc) -> None:
    """A4 版面 + 公文页边距（上 3.7 / 下 3.5 / 左 2.8 / 右 2.6 cm）。"""
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin, sec.bottom_margin, sec.left_margin, sec.right_margin = PAGE_MARGINS


def _setup_normal_style(doc, body_font: str) -> None:
    """Normal 样式默认正文仿宋三号（表格等继承元素的中文底线）。"""
    style = doc.styles["Normal"]
    style.font.name = body_font
    style.font.size = Pt(BODY_SIZE)
    rpr = style.element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), body_font)


def _add_footer_page_numbers(doc) -> None:
    """页脚居中页码 `— N —`（四号宋体，GB/T 9704 惯例）。

    用 w:fldSimple PAGE 域而非静态文本：Word/WPS 打开时按实际页码
    自动计算，增删内容后无需重新导出。
    """
    p = doc.sections[0].footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.line_spacing = Pt(20)

    def _styled(run):
        run.font.name = PAGE_NUMBER_FONT
        run.font.size = Pt(PAGE_NUMBER_SIZE)
        run._element.get_or_add_rPr().get_or_add_rFonts().set(
            qn("w:eastAsia"), PAGE_NUMBER_FONT
        )

    _styled(p.add_run("— "))
    # PAGE 域：查看器打开时动态渲染当前页码
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), " PAGE ")
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rfonts = OxmlElement("w:rFonts")
    for attr in ("w:ascii", "w:eastAsia", "w:hAnsi"):
        rfonts.set(qn(attr), PAGE_NUMBER_FONT)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(PAGE_NUMBER_SIZE * 2)))  # 磅值 → 半磅
    rpr.append(rfonts)
    rpr.append(sz)
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = "1"  # 域结果占位（打开时被实际页码替换）
    r.append(t)
    fld.append(r)
    p._p.append(fld)
    _styled(p.add_run(" —"))


# =========================================================================
# Markdown 解析（块级 + 行内）
# =========================================================================


def _join_soft_lines(lines: list[str]) -> str:
    """软换行拼接：中文直接相接，西文之间补空格。

    LLM 输出与 PDF 抽取都可能在段中硬换行；直接 join("") 会把英文
    单词粘连，join(" ") 又会在中文里插空格（伤害检索与观感）。
    """
    out = lines[0]
    for nxt in lines[1:]:
        if _CJK_RE.search(out[-1:]) and _CJK_RE.search(nxt[:1]):
            out += nxt
        else:
            out += " " + nxt
    return out


def _parse_blocks(text: str) -> list[tuple]:
    """把 Markdown 文本解析为块序列（标题/段落/列表/表格/引用/分割线）。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks: list[tuple] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        m = _HEADING_RE.match(s)
        if m:
            blocks.append(("heading", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue

        if _HR_RE.match(s):
            blocks.append(("hr",))
            i += 1
            continue

        # 表格：当前行是 | 开头、且下一行是分隔行（|---|）
        if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(
            lines[i + 1].strip()
        ):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cell_line = lines[i].strip()
                if not _TABLE_SEP_RE.match(cell_line):
                    cells = [c.strip() for c in cell_line.strip("|").split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue

        m = _UL_RE.match(s)
        if m:
            blocks.append(("ul", m.group(1).strip()))
            i += 1
            continue

        m = _OL_RE.match(s)
        if m:
            blocks.append(("ol", m.group(1), m.group(2).strip()))
            i += 1
            continue

        if s.startswith(">"):
            blocks.append(("quote", s.lstrip(">").strip()))
            i += 1
            continue

        # 普通段落：聚合到空行/新块边界（软换行在出口拼接）
        para = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            nxt2 = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if (
                not nxt
                or _HEADING_RE.match(nxt)
                or _UL_RE.match(nxt)
                or _OL_RE.match(nxt)
                or nxt.startswith("|")
                or nxt.startswith(">")
                or _HR_RE.match(nxt)
                # 落款日期行独立成块（"XX厅\n2026年10月15日"无空行分隔时
                # 不能把日期并进正文段，否则落款右对齐无从识别）
                or _SIGN_DATE_RE.match(nxt)
                # 落款机关名：短行且下一行就是日期 → 提前断段
                or (
                    _SIGN_DATE_RE.match(nxt2)
                    and len(nxt) <= 20
                    and not _END_PUNCT_RE.search(nxt)
                )
            ):
                break
            para.append(nxt)
            i += 1
        blocks.append(("para", _join_soft_lines(para)))
    return blocks


def _mark_signature_blocks(blocks: list[tuple]) -> None:
    """把文档末尾的「机关名 + 日期」标记为右对齐（落款版式）。

    成对识别降低误判：仅当最后一个块是日期行时才右对齐它；其前一个
    块是不超过 20 字、无句末标点的短行时，才一并右对齐。
    """
    if len(blocks) < 2:
        return
    if blocks[-1][0] == "para" and _SIGN_DATE_RE.match(blocks[-1][1].strip()):
        blocks[-1] = ("para_right", blocks[-1][1])
        prev = blocks[-2]
        if (
            prev[0] == "para"
            and len(prev[1]) <= 20
            and not _END_PUNCT_RE.search(prev[1].strip())
        ):
            blocks[-2] = ("para_right", prev[1])


def _add_inline_runs(p, text: str, *, font: str, size: float, bold: bool = False,
                     color: str | None = None) -> None:
    """行内 Markdown → run 序列（**加粗** / *斜体* / `代码`）。

    用 split 保留分隔符（早先用 re.sub 回调丢失上下文，标点归属错乱），
    每段独立建 run，互不污染格式。
    """
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            _set_run_font(p.add_run(part[2:-2]), font, size, bold=True, color=color)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            _set_run_font(p.add_run(part[1:-1]), font, size, bold=bold, italic=True,
                          color=color)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            # 行内代码：等宽 + 暗红（公文里极少出现，仅兜底）
            _set_run_font(p.add_run(part[1:-1]), "Courier New", size, color="C00000")
        else:
            _set_run_font(p.add_run(part), font, size, bold=bold, color=color)


# =========================================================================
# 块级渲染
# =========================================================================


def _add_title(doc, title: str, *, layout: dict) -> None:
    """文档题名（二号小标宋居中；红头模板标题描红）。"""
    p = _new_paragraph(doc, line_spacing=Pt(36), space_before=6, space_after=18)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_font = layout.get("title_font") or TITLE_FONT
    color = RED_HEAD_COLOR if layout.get("red_head") else None
    _set_run_font(p.add_run(title or "公文"), title_font, TITLE_SIZE, bold=False,
                  color=color)


def _add_heading(doc, level: int, text: str, *, layout: dict, body_font: str) -> None:
    """标题层级 → 公文字体映射。

    Markdown `#`/`##` 视为文档一级标题（黑体），`###` → 二级标题（楷体），
    `####` 及以下 → 三级标题（仿宋加粗）。文档题名本身由 _add_title 渲染，
    正文里与题名重复的一级标题在 render_docx 出口跳过。
    """
    if level <= 2:
        font = layout.get("heading_font") or HEADING1_FONT
        bold = False
    elif level == 3:
        font = HEADING2_FONT
        bold = False
    else:
        font = body_font
        bold = True
    p = _new_paragraph(doc, line_spacing=Pt(BODY_LINE_SPACING), space_before=10,
                       space_after=4)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _add_inline_runs(p, text, font=font, size=HEADING_SIZE, bold=bold)


def _add_body_paragraph(doc, text: str, *, right_align: bool = False,
                        indent: bool = True, body_font: str = BODY_FONT) -> None:
    """正文段（三号仿宋，固定行距 28 磅，首行缩进 2 字符，两端对齐）。"""
    p = _new_paragraph(doc, line_spacing=Pt(BODY_LINE_SPACING))
    if right_align:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY  # 公文正文两端对齐
        if indent:
            _set_first_line_indent_chars(p, 200)
    _add_inline_runs(p, text, font=body_font, size=BODY_SIZE)


def _add_list_item(doc, text: str, *, marker: str,
                   body_font: str = BODY_FONT) -> None:
    """列表项（悬挂缩进；项目符号/序号由 marker 给出）。"""
    p = _new_paragraph(doc, line_spacing=Pt(BODY_LINE_SPACING))
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    pf = p.paragraph_format
    pf.left_indent = Pt(32)          # 2 字符（16pt 字号）
    pf.first_line_indent = Pt(-16)   # 悬挂：首行回撤半个符号宽
    _set_run_font(p.add_run(f"{marker} "), body_font, BODY_SIZE)
    _add_inline_runs(p, text, font=body_font, size=BODY_SIZE)


def _add_quote(doc, text: str, *, body_font: str = BODY_FONT) -> None:
    """引用块（楷体 + 左缩进，与正文区分）。"""
    p = _new_paragraph(doc, line_spacing=Pt(BODY_LINE_SPACING))
    p.paragraph_format.left_indent = Pt(32)
    _add_inline_runs(p, text, font=HEADING2_FONT, size=BODY_SIZE, color="595959")


def _add_table(doc, rows: list[list[str]], *, body_font: str = BODY_FONT) -> None:
    """Markdown 表格 → 全框线表格（表头黑体加粗浅灰底，单元格仿宋五号）。"""
    if not rows:
        return
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate(rows):
        for ci in range(cols):
            cell = table.cell(ri, ci)
            text = row[ci] if ci < len(row) else ""
            # 表头：黑体加粗 + 底纹；数据行：模板正文字体
            font = TABLE_HEADER_FONT if ri == 0 else body_font
            bold = ri == 0
            if ri == 0:
                tcpr = cell._tc.get_or_add_tcPr()
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:fill"), TABLE_HEADER_FILL)
                tcpr.append(shd)
            cp = cell.paragraphs[0]
            cp.paragraph_format.line_spacing = Pt(18)
            cp.paragraph_format.space_after = Pt(2)
            _add_inline_runs(cp, text, font=font, size=TABLE_SIZE, bold=bold)
    # 表后空一行，避免与后续正文贴太近
    _new_paragraph(doc, line_spacing=Pt(8))


def _add_references(doc, citations: list, *, body_font: str = BODY_FONT) -> None:
    """参考资料区（标题黑体三号 + 条目仿宋四号）。"""
    p = _new_paragraph(doc, line_spacing=Pt(BODY_LINE_SPACING), space_before=14,
                       space_after=6)
    _set_run_font(p.add_run("参考资料"), HEADING1_FONT, HEADING_SIZE)
    for i, c in enumerate(citations, 1):
        if not isinstance(c, dict):
            continue
        rp = _new_paragraph(doc, line_spacing=Pt(22))
        rp.paragraph_format.left_indent = Pt(32)
        rp.paragraph_format.first_line_indent = Pt(-32)
        title = c.get("title") or "未知"
        doc_no = c.get("doc_no") or ""
        _set_run_font(
            rp.add_run(f"[{i}] 《{title}》{doc_no}"),
            body_font,
            14,
        )


def _add_sensitive_note(doc, hits: list[str]) -> None:
    """脱敏说明（楷体小字，附于文末，便于归档溯源）。"""
    p = _new_paragraph(doc, line_spacing=Pt(20), space_before=10)
    _set_run_font(
        p.add_run(f"【敏感词命中】{', '.join(hits)}（已脱敏）"),
        HEADING2_FONT,
        12,
        color="C00000",
    )


# =========================================================================
# 对外主入口
# =========================================================================


def render_docx(
    *,
    title: str,
    content: str,
    citations: list | None = None,
    sensitive_hits: list[str] | None = None,
    layout: dict | None = None,
) -> bytes:
    """把已生成的正文渲染为公文版式 docx 字节流。

    Args:
        title: 文档题名（会话标题；渲染为二号小标宋居中标题）。
        content: 正文（Markdown；LLM 生成结果即此格式）。
        citations: 引用列表（[{"title": ..., "doc_no": ...}, ...]），
            渲染为文末「参考资料」区。
        sensitive_hits: 敏感词命中（渲染为文末脱敏说明）。
        layout: 模板版式（可选）：`font`（正文字体覆盖）、
            `title_font` / `heading_font`（标题字体覆盖）、
            `red_head`（红头标题）、`paper`（预留，当前仅 A4）。

    Returns:
        docx 文件字节流（调用方直接作为 HTTP 响应体返回）。
    """
    layout = layout or {}
    body_font = layout.get("font") or BODY_FONT

    doc = DocxDocument()
    _setup_page(doc)
    _setup_normal_style(doc, body_font)
    _add_title(doc, title, layout=layout)

    blocks = _parse_blocks(content or "")
    _mark_signature_blocks(blocks)

    title_norm = re.sub(r"[\s，。、；：（）()【】]", "", title or "")
    first_para = True  # 主送机关顶格仅对首个自然段生效
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            level, text = block[1], block[2]
            # 正文首个一级标题（#/##）与题名重复时跳过（题名已由标题渲染）：
            # LLM 常把题名同时作为正文第一个 md 标题输出，不去重会双重标题
            if level <= 2 and re.sub(r"[\s，。、；：（）()【】]", "", text) == title_norm:
                continue
            _add_heading(doc, level, text, layout=layout, body_font=body_font)
        elif kind == "para":
            # 整段加粗的短行按小标题处理（黑体，不缩进）——LLM 常用写法
            if _BOLD_PARA_RE.match(block[1]) and len(block[1]) <= 40:
                _add_heading(doc, 2, block[1].strip("*"), layout=layout,
                             body_font=body_font)
                continue
            # 主送机关（首个自然段且以冒号结尾）顶格
            top_align = (
                first_para
                and block[1].rstrip().endswith(("：", ":"))
                and len(block[1]) <= 40
            )
            _add_body_paragraph(doc, block[1], indent=not top_align,
                                body_font=body_font)
        elif kind == "para_right":
            _add_body_paragraph(doc, block[1], right_align=True, indent=False,
                                body_font=body_font)
        elif kind == "ul":
            _add_list_item(doc, block[1], marker="•", body_font=body_font)
        elif kind == "ol":
            _add_list_item(doc, block[2], marker=f"{block[1]}.", body_font=body_font)
        elif kind == "table":
            _add_table(doc, block[1], body_font=body_font)
        elif kind == "quote":
            _add_quote(doc, block[1], body_font=body_font)
        elif kind == "hr":
            _new_paragraph(doc, line_spacing=Pt(10))
        if kind in ("para", "para_right"):
            first_para = False

    if citations:
        _add_references(doc, citations, body_font=body_font)
    if sensitive_hits:
        _add_sensitive_note(doc, sensitive_hits)

    _add_footer_page_numbers(doc)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
