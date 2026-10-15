"""主编排：OFD 字节流 → 清洗后的正文文本。

流水线（与主服务《文档解析与切分重构方案 V2.0》阶段 2-3 对应）：

1. ``OFDPackage`` 导航页内容文件（reader.py）；
2. 逐页提取 TextCode 文本片段（TextObject 边界坐标兜底）；
3. 坐标阅读顺序重排为行（textorder.py）；
4. 页码/页眉/页脚剔除（noise.py，可关）；
5. 字体映射错位修复 + 规范化（normalize.py，可关）；
6. 扫描件判定（无文字层页计入 scanned_pages，OCR 兜底信号）。
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ofdkit.model import ExtractResult, OFDPage, TextRun
from ofdkit.noise import Line, filter_noise_lines
from ofdkit.normalize import normalize_text
from ofdkit.reader import OFDPackage, _iter_local, _local
from ofdkit.textorder import runs_to_lines

# 无文字层页阈值：不足 5 字符视为无文字层（签章页/扫描页/图片页）
_MIN_PAGE_CHARS = 5
# 未知页高时的 A4 兜底（mm）——仅影响页眉/脚分区比例，不影响正文
_DEFAULT_PAGE_HEIGHT = 297.0


def _page_runs(croot, page_index: int) -> list[TextRun]:
    """从单个 Content.xml 提取文本片段。

    **坐标语义（GB/T 33190-2016，2026-10-15 实测修正）**：
    ``TextCode`` 的 ``X``/``Y`` 是**相对父级 TextObject Boundary 左上角**
    的局部偏移（mm），不是页面绝对坐标。真实公文的 TextCode 普遍写作
    ``X="0.0" Y="6.0"``（相对绘制原点），若把它当绝对坐标，所有片段都会
    堆到页面顶部 → 阅读顺序重排失效（实测：细粒度片段文件正文完全乱序）。

    因此：只要父级是 TextObject 且 Boundary 有效，就必须做
    ``绝对坐标 = Boundary 左上角 + 局部偏移``；仅在无父级 Boundary 时
    才单独使用 X/Y。

    无 TextObject 包裹（畸形文件）时退化为 (0,0)——至少不崩。
    """
    # 一次性建父子索引（O(n)），避免逐元素回溯（旧实现 O(n²) 踩坑）
    parent_of: dict[int, object] = {}
    for el in croot.iter():
        for child in el:
            parent_of[id(child)] = el

    runs: list[TextRun] = []
    for tc in _iter_local(croot, "TextCode"):
        text = (tc.text or "").strip()
        if not text:
            continue

        def _num(node, key: str) -> float | None:
            v = node.get(key)
            try:
                return float(v) if v is not None else None
            except ValueError:
                return None

        # TextCode 自身 X/Y：局部偏移（可能缺失）
        x = _num(tc, "X") or 0.0
        y = _num(tc, "Y") or 0.0
        # 父级 TextObject Boundary：页面绝对坐标基准
        parent = parent_of.get(id(tc))
        if parent is not None and _local(parent.tag) == "TextObject":
            box = (parent.get("Boundary") or "").split()
            if len(box) == 4:
                try:
                    x = float(box[0]) + x
                    y = float(box[1]) + y
                except ValueError:
                    pass  # Boundary 畸形：保留局部偏移（降级但不丢文本）
        runs.append(TextRun(text=text, x=x, y=y, page=page_index))
    return runs


def extract_pages(data: bytes) -> list[OFDPage]:
    """低层 API：逐页提取原始行（未做噪音清洗与规范化），供调试/测试。"""
    pages: list[OFDPage] = []
    with OFDPackage(data) as pkg:
        for idx, ref in enumerate(pkg.pages()):
            try:
                croot = ET.fromstring(pkg.read(ref.entry))
            except Exception:  # noqa: BLE001 —— 单页损坏不阻断其余页
                pages.append(OFDPage(index=idx, height=None))
                continue
            runs = _page_runs(croot, idx)
            lines = runs_to_lines(runs)
            height = ref.physical_box[1] if ref.physical_box else None
            pages.append(
                OFDPage(
                    index=idx,
                    lines=[t for t, _ in lines],
                    line_ys=[y for _, y in lines],
                    height=height,
                )
            )
    return pages


def extract_text(
    data: bytes,
    *,
    clean_noise: bool = True,
    fix_glyphs: bool = True,
) -> ExtractResult:
    """完整提取：字节流 → 清洗后的正文 + 统计信息。

    Args:
        data: OFD 文件字节流。
        clean_noise: 是否剔除页码/页眉/页脚（默认开；关闭后原文直出，
            供对照审计）。
        fix_glyphs: 是否做字体映射错位修复与规范化（默认开）。

    Returns:
        ExtractResult。无文字层的整体为扫描件（is_scanned=True），
        文字层缺失的页计入 scanned_pages。
    """
    pages = extract_pages(data)
    if not pages:
        return ExtractResult(text="", pages=0, is_scanned=True, scanned_pages=0)

    # 行序列 + 页高（用于页眉/脚分区；页高缺失按 A4 兜底）
    all_lines: list[Line] = []
    heights: dict[int, float] = {}
    for pg in pages:
        for text, y in zip(pg.lines, pg.line_ys, strict=True):
            all_lines.append(Line(text=text, page=pg.index, y=y))
        heights[pg.index] = pg.height or _DEFAULT_PAGE_HEIGHT

    removed: list[str] = []
    if clean_noise:
        kept, removed = filter_noise_lines(all_lines, heights)
    else:
        kept = all_lines

    # 按页拼接：页内行用 \n，页间用 \n（假边界由上层段落重建消化）
    page_lines: dict[int, list[str]] = {}
    for ln in kept:
        page_lines.setdefault(ln.page, []).append(ln.text)

    scanned = 0
    page_texts: list[str] = []
    for pg in pages:
        lines = page_lines.get(pg.index, [])
        body = "\n".join(lines)
        if len(body.strip()) < _MIN_PAGE_CHARS:
            scanned += 1
            body = ""  # 无文字层：留待 OCR，不占位
        page_texts.append(body)

    text = "\n".join(t for t in page_texts if t)
    if fix_glyphs:
        text = normalize_text(text)

    return ExtractResult(
        text=text,
        pages=len(pages),
        is_scanned=scanned >= max(1, len(pages) // 2),
        scanned_pages=scanned,
        removed_lines=removed,
    )
