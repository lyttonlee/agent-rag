"""版式噪音（页码/页眉/页脚）检测与剔除——PDF 与 OFD 共用的格式无关算法。

两类噪音来源（2026-10-15 实测 30 篇政府公报 PDF 确认）：

1. **页码**：页脚的 `— １ —`（破折线包裹的全角页码，提取后常被拆成三行）、
   `第 3 页`、`3 / 8`、纯数字行、罗马数字行。页码进入 embedding/BM25
   会污染检索，且页间拼接产生假段落边界；
2. **页眉/页脚固定文字**：如 `四川省人民政府办公厅文件` 每页页眉重复出现，
   属于版式信息而非正文。

判定策略（保守优先，防误删正文）：

- **位置分区**：页眉区 = 页高前 8%，页脚区 = 后 8%。正文行落入该区域
  **不必然删除**——必须同时满足下述条件之一；
- **页码模式**：强模式（`— 1 —` / `第X页` / `X/Y`）在任意位置即删；
  弱模式（纯数字/罗马数字）仅当位于页眉/脚区才删（防误删正文序号）。
  页码常被提取器拆成多行（`—`/`１`/`—`），故页眉/脚区内先做相邻行
  合并尝试，仅当合并文本命中强页码模式才替换；
- **跨页高频重复**：规范化文本相同、且落在页眉区或页脚区、出现于
  ≥50% 的页 → 判为页眉/页脚行删除。**正文区不判重**——每页都出现的
  章节标题恰好在页中时不误伤。

该模块不解析任何文件格式，只消费 (text, page, y) 行序列，PDF 侧由
PyMuPDF 提供同样的行序列即可复用（坐标单位随格式不同，但全部逻辑
均为比例/形态判定，单位无关）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 页码强模式：版式特征 unmistakable，任意位置命中即删
_STRONG_PAGE_NUM_RES = (
    re.compile(r"^[—–\-]\s*\d{1,4}\s*[—–\-]$"),  # — 1 —（\d 兼容全角数字）
    re.compile(r"^第\s*\d{1,4}\s*页$"),  # 第 3 页
    re.compile(r"^第\s*\d{1,4}\s*页\s*/?\s*共?\s*\d{1,4}\s*页?$"),  # 第3页/共8页
    re.compile(r"^\d{1,4}\s*/\s*\d{1,4}$"),  # 3 / 8
)
# 页码弱模式：仅在页眉/脚区命中才删（正文区同形行可能是序号）
_WEAK_PAGE_NUM_RES = (
    re.compile(r"^\d{1,4}$"),  # 纯数字行（半角）
    re.compile(r"^[０-９]{1,4}$"),  # 纯数字行（全角）
    re.compile(r"^[IVXLCM]{1,6}$"),  # 罗马数字行
)


# 行模型：text=行文本，page=页码（0 起），y=行顶坐标（原点左上、向下增大）
@dataclass(frozen=True)
class Line:
    text: str
    page: int
    y: float


def _norm_for_repeat(text: str) -> str:
    """跨页判重的规范化：去全部空白，统一小写（页眉常有空格差异）。"""
    return re.sub(r"\s+", "", text).lower()


def _zone_of(ln: Line, height: float | None, header_ratio: float, footer_ratio: float) -> str:
    """行所在分区：'header' / 'footer' / 'body' / 'unknown'（页高缺失）。"""
    if not height or height <= 0:
        return "unknown"
    if ln.y < height * header_ratio:
        return "header"
    if ln.y > height * (1.0 - footer_ratio):
        return "footer"
    return "body"


def is_page_number(text: str, *, in_margin_zone: bool) -> bool:
    """判断一行是否为页码。

    Args:
        text: 行文本。
        in_margin_zone: 该行是否位于页眉区或页脚区（位置分区信号）。
            False 时仅强模式可判页码——正文区的 `3` 可能是列表序号，
            不能凭形态删除。
    """
    t = text.strip()
    if not t:
        return False
    if any(rx.match(t) for rx in _STRONG_PAGE_NUM_RES):
        return True
    if _is_dash_digit_cluster(t):
        return True
    return bool(in_margin_zone and any(rx.match(t) for rx in _WEAK_PAGE_NUM_RES))


def _is_strong_page_number(text: str) -> bool:
    return any(rx.match(text.strip()) for rx in _STRONG_PAGE_NUM_RES)


def _is_dash_digit_cluster(text: str) -> bool:
    """破折线 + 数字簇（任意顺序）：页码的强特征。

    实测 PDF 页码 `— １ —` 被提取器拆成三行且 Y 排序为 `1`/`—`/`—`
    （破折线垂直居中于数字，bbox 顶比数字低），合并文本呈 `1——` 等
    任意顺序，固定顺序的正则无法覆盖。判定放宽为：去破折线与空白后
    仅剩 1-4 位数字且原文至少含一条破折线——该形态在公文中只可能是
    页码，不会误伤正文。
    """
    t = text.strip()
    if not t or not any(c in t for c in "—–-"):
        return False
    rest = re.sub(r"[—–\-\s]", "", t)
    return bool(rest) and rest.isdigit() and len(rest) <= 4


def _merge_page_number_lines(
    lines: list[Line],
    *,
    edge_lines: int = 4,
) -> list[Line]:
    """页首/页尾区域的相邻行合并（页码拆行的修复前置）。

    实测：PDF 页码 `— １ —` 被 PyMuPDF 拆成三行，且 Y 排序为
    `２`/`—`/`—`（破折线垂直居中于数字，bbox 顶比数字低），合并文本
    呈 `２——` 等任意顺序；页码位置在页高 90% 处（公文页脚留白大），
    固定比例的分区未必覆盖。故不依赖分区比例，直接取每页**首/尾各
    ``edge_lines`` 行**做 2~3 行合并尝试——页码只可能出现在页面边缘。

    安全边界：**仅当合并文本命中页码强模式（含破折线数字簇）才替换**，
    合并不改变文本内容，只是还原"被拆散的页码"形态；正文行任何组合
    都不可能变成页码形态，故不会误伤。
    """
    by_page: dict[int, list[Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)

    out: list[Line] = []
    for pno, plns in by_page.items():
        n = len(plns)
        if n < 2:
            out.extend(plns)
            continue
        head = set(range(min(edge_lines, n)))
        tail = set(range(max(0, n - edge_lines), n))
        edge = head | tail
        i = 0
        while i < n:
            if i in edge:
                merged: Line | None = None
                size = 1  # 无合并命中时按单行处理（防御 range 为空）
                for size in range(3, 1, -1):  # 先试 3 行合并，再 2 行
                    if i + size > n:
                        continue
                    cand = "".join(plns[k].text for k in range(i, i + size))
                    if _is_strong_page_number(cand) or _is_dash_digit_cluster(cand):
                        merged = Line(text=cand, page=pno, y=plns[i].y)
                        break
                if merged is not None:
                    out.append(merged)
                    i += size
                    continue
            out.append(plns[i])
            i += 1
    return out


def filter_noise_lines(
    lines: list[Line],
    page_heights: dict[int, float],
    *,
    header_ratio: float = 0.10,
    footer_ratio: float = 0.12,
    repeat_threshold: float = 0.5,
    edge_lines: int = 4,
) -> tuple[list[Line], list[str]]:
    """剔除页码/页眉/页脚行，返回 (保留行, 被删行文本)。

    算法三遍扫描：
    0. 页首/页尾区域相邻行合并（还原被拆散的页码形态，如 `２`/`—`/`—`）；
    1. 页码模式剔除（强模式全局 + 弱模式限页眉/脚区）；
    2. 跨页高频重复剔除（仅页眉/脚区判重，出现页数 ≥ repeat_threshold × 总页数）。

    Args:
        lines: 全文行序列（按文档顺序）。
        page_heights: {页码: 页高}；缺页高的页不做分区判定（仅强模式页码仍删）。
        header_ratio / footer_ratio: 页眉/脚区占页高比例（公文页脚留白大，
            实测页码位于页高 90% 处，故页脚区取 12%）。
        repeat_threshold: 跨页判重阈值（0~1）。
        edge_lines: 页码合并考察的页首/页尾行数。

    Returns:
        (保留行, 被删行文本列表)——被删行原文保留供上层审计。
    """
    if not lines:
        return [], []

    total_pages = len({ln.page for ln in lines}) or 1
    lines = _merge_page_number_lines(lines, edge_lines=edge_lines)
    zones = {id(ln): _zone_of(ln, page_heights.get(ln.page), header_ratio, footer_ratio) for ln in lines}

    # ---- 第一遍：页码模式 ----
    kept: list[Line] = []
    removed: list[str] = []
    for ln in lines:
        z = zones[id(ln)]
        if is_page_number(ln.text, in_margin_zone=z in ("header", "footer")):
            removed.append(ln.text)
        else:
            kept.append(ln)

    # ---- 第二遍：跨页高频重复（仅页眉/脚区）----
    repeat_pages: dict[tuple[str, str], set[int]] = {}
    for ln in kept:
        z = zones[id(ln)]
        if z in ("header", "footer"):
            key = (_norm_for_repeat(ln.text), z)
            repeat_pages.setdefault(key, set()).add(ln.page)
    noise_keys = {
        key for key, pages in repeat_pages.items() if len(pages) >= max(2, total_pages * repeat_threshold)
    }
    if noise_keys:
        final: list[Line] = []
        for ln in kept:
            z = zones[id(ln)]
            key = (_norm_for_repeat(ln.text), z)
            if z in ("header", "footer") and key in noise_keys:
                removed.append(ln.text)
            else:
                final.append(ln)
        kept = final

    return kept, removed
