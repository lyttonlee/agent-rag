"""PDF 测试样本构造器：用 PyMuPDF 构造带页码/页眉的多页中文 PDF。

china-s 为 PyMuPDF 内置简体中文字体（Droid Sans Fallback 替代），
离线可用；页码用 helv（Base14）写 ASCII，模拟真实公文页码形态。
"""

from __future__ import annotations

import pymupdf

PAGE_W, PAGE_H = 595.0, 842.0  # A4（pt）
CJK = "china-s"


def make_pdf(
    pages: list[list[tuple[float, float, str]]],
    *,
    header: str | None = None,
    page_number: bool = True,
    width: float = PAGE_W,
    height: float = PAGE_H,
) -> bytes:
    """构造多页 PDF。

    Args:
        pages: 每页的 [(x, y, text), ...] 正文行（y 为基线，原点左上）。
        header: 每页页眉文本（写在页高 7% 处，测跨页重复剔除）。
        page_number: 是否在页脚（页高 95% 处）写 `- N -` 页码。
    """
    doc = pymupdf.open()
    for i, lines in enumerate(pages, start=1):
        page = doc.new_page(width=width, height=height)
        if header:
            page.insert_text((72, height * 0.07), header, fontname=CJK, fontsize=10)
        for x, y, text in lines:
            page.insert_text((x, y), text, fontname=CJK, fontsize=11)
        if page_number:
            page.insert_text((width / 2 - 15, height * 0.95), f"- {i} -", fontname="helv", fontsize=10)
    return doc.tobytes()
