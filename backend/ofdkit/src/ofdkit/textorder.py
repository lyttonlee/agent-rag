"""坐标阅读顺序重排：TextRun 序列 → 行文本序列。

OFD 的文字位于 <TextCode>，其**文档顺序未必等于阅读顺序**（分栏、右起
排版、签注层穿插都会乱序）。每个 TextCode 带基线起点 (X, Y)（mm，原点
左上、Y 向下），因此：

1. 先按 (page, Y, X) 排序——同一基线的片段天然相邻；
2. Y 相近（容差 y_tolerance，默认 1.5mm）的片段合并为一行，行内按 X 排序；
3. Y 跳变超过容差 → 换行。

容差取值依据：四号字（14pt≈4.9mm）行距约 6mm，1.5mm 容差不会误合相邻行；
小于最小字号半高，不会漏合同行片段。
"""

from __future__ import annotations

from ofdkit.model import TextRun

DEFAULT_Y_TOLERANCE = 1.5  # mm：行合并容差


def runs_to_lines(
    runs: list[TextRun],
    *,
    y_tolerance: float = DEFAULT_Y_TOLERANCE,
) -> list[tuple[str, float]]:
    """把文本片段序列重排为阅读顺序的行。

    Args:
        runs: 全部页的文本片段（可跨页，按 page 分组处理）。
        y_tolerance: Y 相近判定容差（mm）。

    Returns:
        [(行文本, 行顶Y坐标), ...]，按阅读顺序。行顶 Y = 该行首个
        （排序后）片段的 Y，供页眉/页脚分区判定使用。
    """
    if not runs:
        return []
    # page → 该页片段；页间保持页码顺序
    by_page: dict[int, list[TextRun]] = {}
    for r in runs:
        by_page.setdefault(r.page, []).append(r)

    lines: list[tuple[str, float]] = []
    for page in sorted(by_page):
        page_runs = sorted(by_page[page], key=lambda r: (r.y, r.x))
        cur_y: float | None = None
        cur: list[tuple[float, str]] = []  # (x, text)，行内缓冲
        for r in page_runs:
            if cur_y is None:
                cur_y = r.y
            if abs(r.y - cur_y) > y_tolerance and cur:
                lines.append(("".join(t for _, t in sorted(cur)), cur_y))
                cur, cur_y = [], r.y
            cur.append((r.x, r.text))
        if cur:
            lines.append(("".join(t for _, t in sorted(cur)), cur_y or 0.0))
    return lines
