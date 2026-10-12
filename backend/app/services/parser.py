"""文档解析：PDF / DOCX 文本提取 + 扫描件判定。

设计（详见《详细功能设计文档》）：
- 优先提取文字层；无文字层（扫描件）标记为需 OCR（MVP 留后路，非必做）。
- 真实样本体检显示 96.7% 为带文字层电子版，OCR 非瓶颈。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf  # PyMuPDF（新入口，fitz 已弃用）
from docx import Document as DocxDocument


@dataclass
class ParsedDoc:
    text: str
    pages: int
    is_scanned: bool          # 是否纯扫描件（无文字层）
    scanned_pages: int


def parse_pdf_bytes(data: bytes) -> ParsedDoc:
    """解析 PDF 字节流，判定扫描件（逐页检测文字层）。"""
    doc = pymupdf.open(stream=data, filetype="pdf")
    texts: list[str] = []
    scanned_pages = 0
    for page in doc:
        t = page.get_text("text").strip()
        if len(t) < 5:  # 阈值：近乎无文字视为扫描页
            scanned_pages += 1
            t = ""  # 留待 OCR
        texts.append(t)
    full = "\n".join(texts)
    is_scanned = scanned_pages >= max(1, doc.page_count // 2)
    return ParsedDoc(text=full, pages=doc.page_count, is_scanned=is_scanned, scanned_pages=scanned_pages)


def parse_docx_bytes(data: bytes) -> ParsedDoc:
    """解析 DOCX 字节流。"""
    doc = DocxDocument(io.BytesIO(data))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    return ParsedDoc(text="\n".join(paras), pages=1, is_scanned=False, scanned_pages=0)


def _sniff_format(data: bytes) -> str:
    """按文件头（magic bytes）嗅探真实格式，兼容「无扩展名」文件。"""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    # DOCX / OOXML 本质是 ZIP 包（PK\x03\x04）
    if head.startswith(b"PK\x03\x04"):
        return "docx"
    return ""


def parse_bytes(filename: str, data: bytes) -> ParsedDoc:
    """按扩展名（优先）或内容嗅探分发解析。

    说明：公文系统导出的文件常不带扩展名，故在无扩展名时回退到
    文件头嗅探，保证 PDF / DOCX 仍可被正确解析。
    """
    lower = filename.lower()
    fmt = ""
    if lower.endswith(".pdf"):
        fmt = "pdf"
    elif lower.endswith((".docx", ".doc")):
        fmt = "docx"
    else:
        fmt = _sniff_format(data)  # 无扩展名或未知后缀时嗅探
    if fmt == "pdf":
        return parse_pdf_bytes(data)
    if fmt == "docx":
        return parse_docx_bytes(data)
    # 其它格式（wps/ofd 等）二期或经格式适配器预处理
    raise ValueError(f"暂不支持的解析格式：{filename}")
