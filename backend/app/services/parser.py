"""文档解析：PDF / DOCX / TXT / OFD 文本提取 + 扫描件判定。

设计（详见《文档解析与切分重构方案_V2.0》）：
- 优先提取文字层；无文字层（扫描件/无文字层 OFD 页）标记为需 OCR。
- **位置感知提取**（PDF/OFD）：逐行取文本 + 坐标，页码/页眉/页脚经
  ``ofdkit.noise.filter_noise_lines`` 剔除后再拼正文——版式噪音进入
  embedding/BM25 会污染检索，且页间拼接会产生假段落边界。
- **乱码码点规范化**：方正书版系 PDF/OFD 把中文标点映射到错误 Unicode
  区段（如句号变 ``ꎮ``），全语料实测 4300+ 处，不修复则句子边界识别
  全面失效。映射表与规范化逻辑收敛于 ofdkit（单一事实源）。
- OFD 解析**委托独立库 ofdkit**（backend/ofdkit/，零三方依赖，可独立
  发版/被部署方复用），本模块不再内嵌 OFD 实现。
- TXT/MD 为纯文本直读（兼容 UTF-8 / GBK / BOM），主要满足模板文件上传。

``noise_clean`` 开关（配置中心 ``parse_noise_clean``，默认开）：
关后回归"原文直出"，供对照审计与特殊场景兼容。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf  # PyMuPDF（新入口，fitz 已弃用）
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from ofdkit.noise import Line, filter_noise_lines
from ofdkit.normalize import normalize_text

from ofdkit import extract_text as ofd_extract_text
from ofdkit import looks_like_ofd

# 近乎无文字的页视为无文字层页（扫描页/签章页）：留待 OCR
_MIN_PAGE_CHARS = 5


@dataclass
class ParsedDoc:
    text: str
    pages: int
    is_scanned: bool  # 是否纯扫描件（无文字层）
    scanned_pages: int


# ---------------------------------------------------------------------------
# PDF：位置感知逐行提取 + 页码/页眉页脚剔除
# ---------------------------------------------------------------------------


def _pdf_page_lines(page: pymupdf.Page, page_no: int) -> list[Line]:
    """用 dict 模式提取单页的文本行（行 + 行顶 Y 坐标）。

    为什么不用 ``get_text("text")``：纯文本模式丢失坐标，无法做页眉/脚
    分区判定与页码剔除。dict 模式给出每个 line 的 bbox（PyMuPDF 坐标系
    原点在页面左上、Y 向下，与 ofdkit 的 mm 坐标系同向，比例类逻辑通用）。

    图片块（type=1）不含文字直接跳过；行内多个 span 拼接（PDF 常把一
    行拆成多个 span，尤其跨字体时）。
    """
    lines: list[Line] = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:  # 0=文本块，1=图片块
            continue
        for ln in block.get("lines", []):
            text = "".join(span.get("text", "") for span in ln.get("spans", []))
            text = text.strip()
            if not text:
                continue
            y = float(ln["bbox"][1])  # 行顶 Y
            lines.append(Line(text=text, page=page_no, y=y))
    lines.sort(key=lambda ln: ln.y)
    return lines


def parse_pdf_bytes(data: bytes, *, noise_clean: bool = True) -> ParsedDoc:
    """解析 PDF 字节流：位置感知提取 → 噪音剔除 → 规范化 → 扫描件判定。

    页眉/页脚剔除需要**全文行序列**（跨页判重），故先收齐全部页的行，
    再统一过滤，最后按页重组。
    """
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages_lines: list[list[Line]] = []
        heights: dict[int, float] = {}
        for pno, page in enumerate(doc):
            heights[pno] = float(page.rect.height)
            pages_lines.append(_pdf_page_lines(page, pno))
    finally:
        doc.close()

    all_lines = [ln for pls in pages_lines for ln in pls]
    kept = filter_noise_lines(all_lines, heights)[0] if noise_clean else all_lines

    kept_by_page: dict[int, list[str]] = {}
    for ln in kept:
        kept_by_page.setdefault(ln.page, []).append(ln.text)

    scanned = 0
    page_texts: list[str] = []
    for pno in range(len(pages_lines)):
        body = "\n".join(kept_by_page.get(pno, []))
        if len(body.strip()) < _MIN_PAGE_CHARS:
            scanned += 1
            body = ""  # 无文字层：留待 OCR，不占位
        page_texts.append(body)

    full = "\n".join(t for t in page_texts if t)
    if noise_clean:
        full = normalize_text(full)
    n_pages = len(pages_lines)
    return ParsedDoc(
        text=full,
        pages=n_pages,
        is_scanned=scanned >= max(1, n_pages // 2),
        scanned_pages=scanned,
    )


# ---------------------------------------------------------------------------
# DOCX：按 body 顺序提取段落 + 表格（doc.paragraphs 会漏掉表格内容）
# ---------------------------------------------------------------------------


def _docx_table_lines(tbl: DocxTable) -> list[str]:
    """表格转文本行：表头行 + 数据行，数据行转为「表头：值」对。

    公文 DOCX 常含任免表/指标表，``doc.paragraphs`` 拿不到表格内容；
    且跨页表格会重复表头行，需去重。单列表格退化为纯值行。
    """
    rows: list[list[str]] = []
    for row in tbl.rows:
        cells = [c.text.strip() for c in row.cells]
        if any(cells):
            rows.append(cells)
    if not rows:
        return []
    if len(rows) == 1:
        return [" | ".join(rows[0])]
    header = rows[0]
    out: list[str] = []
    for r in rows[1:]:
        if r == header:  # 跨页重复表头
            continue
        pairs = [f"{h}：{v}" for h, v in zip(header, r, strict=False) if v]
        out.append("；".join(pairs) if pairs else " | ".join(r))
    return out


def parse_docx_bytes(data: bytes, *, noise_clean: bool = True) -> ParsedDoc:
    """解析 DOCX 字节流：按 body 文档顺序迭代段落与表格。

    段落之间以**空行**连接（``\\n\\n``）——DOCX 的段落边界是显式事实，
    用空行传递给下游段落重建（空行=最高优先级边界），避免软换行推断
    把两个完整段落误粘一段。
    """
    doc = DocxDocument(io.BytesIO(data))
    blocks: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = DocxParagraph(child, doc).text.strip()
            if text:
                blocks.append(text)
        elif tag == "tbl":
            blocks.extend(_docx_table_lines(DocxTable(child, doc)))
    full = "\n\n".join(blocks)
    if noise_clean:
        full = normalize_text(full)
    return ParsedDoc(text=full, pages=1, is_scanned=False, scanned_pages=0)


# ---------------------------------------------------------------------------
# TXT / MD：纯文本直读
# ---------------------------------------------------------------------------


def parse_text_bytes(data: bytes, *, noise_clean: bool = True) -> ParsedDoc:
    """解析纯文本（TXT/MD）：兼容 UTF-8 / GBK / BOM 三种常见公文导出形态。"""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]  # BOM：先剥再解码，否则首段带不可见字符
    for enc in ("utf-8", "gbk"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if noise_clean:
        text = normalize_text(text)
    return ParsedDoc(text=text, pages=1, is_scanned=False, scanned_pages=0)


# ---------------------------------------------------------------------------
# OFD：委托独立库 ofdkit（本模块不再内嵌 OFD 解析实现）
# ---------------------------------------------------------------------------


def parse_ofd_bytes(data: bytes, *, noise_clean: bool = True) -> ParsedDoc:
    """解析 OFD（GB/T 33190）：薄封装，逻辑全部在 ofdkit 独立库。

    ofdkit 负责：ZIP 包导航 → 坐标阅读顺序重排 → 页码/页眉页脚剔除 →
    乱码码点规范化 → 扫描件判定。库零三方依赖，可被部署方单独复用。
    """
    result = ofd_extract_text(data, clean_noise=noise_clean, fix_glyphs=noise_clean)
    return ParsedDoc(
        text=result.text,
        pages=result.pages,
        is_scanned=result.is_scanned,
        scanned_pages=result.scanned_pages,
    )


# ---------------------------------------------------------------------------
# 分发：扩展名优先，无扩展名回退 magic bytes 嗅探
# ---------------------------------------------------------------------------


def _sniff_format(data: bytes) -> str:
    """按文件头（magic bytes）嗅探真实格式，兼容「无扩展名」文件。"""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    # DOCX / OFD 本质都是 ZIP 包（PK\x03\x04）——需解包看内部 XML 命名空间区分，
    # 不能仅凭魔数判定为 docx（否则 OFD 被 python-docx 误解析）。
    if head.startswith(b"PK\x03\x04"):
        return "ofd" if looks_like_ofd(data) else "docx"
    return ""


def parse_bytes(filename: str, data: bytes, *, noise_clean: bool = True) -> ParsedDoc:
    """按扩展名（优先）或内容嗅探分发解析。

    说明：公文系统导出的文件常不带扩展名，故在无扩展名时回退到
    文件头嗅探，保证 PDF / DOCX 仍可被正确解析；TXT/MD 按扩展名直读。

    Args:
        filename: 文件名（含扩展名；无扩展名时走嗅探）。
        data: 文件字节流。
        noise_clean: 是否做页码/页眉清洗与乱码规范化（配置中心
            ``parse_noise_clean``，默认开）。
    """
    lower = filename.lower()
    if lower.endswith(".pdf"):
        fmt = "pdf"
    elif lower.endswith((".docx", ".doc")):
        fmt = "docx"
    elif lower.endswith(".ofd"):
        fmt = "ofd"
    elif lower.endswith((".txt", ".md")):
        fmt = "text"
    else:
        fmt = _sniff_format(data)
    if fmt == "pdf":
        return parse_pdf_bytes(data, noise_clean=noise_clean)
    if fmt == "docx":
        return parse_docx_bytes(data, noise_clean=noise_clean)
    if fmt == "ofd":
        return parse_ofd_bytes(data, noise_clean=noise_clean)
    if fmt == "text":
        return parse_text_bytes(data, noise_clean=noise_clean)
    # 其它格式（wps 等）二期或经格式适配器预处理。
    # 报错列出全部支持格式：调用方（入库任务）会把该消息原样落库到
    # job.error_message，是用户/运维判断"为何这个文件入不了库"的唯一线索，
    # 因此宁可啰嗦也不能只说"不支持"。
    supported = "PDF / DOCX(.doc) / OFD / TXT(.md)"
    raise ValueError(f"暂不支持的解析格式：{filename}（支持：{supported}）")
