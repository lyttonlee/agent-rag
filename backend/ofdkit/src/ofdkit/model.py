"""ofdkit 数据模型：文本运行 / OFD 页 / 提取结果。

所有坐标单位为毫米（mm），原点位于页面左上角，Y 向下增大——与 OFD 规范
（GB/T 33190）一致。阅读顺序、噪音判定全部基于该坐标系。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextRun:
    """单个文本片段（一个 TextCode 元素）：文本 + 基线起点坐标 + 所属页。"""

    text: str
    x: float
    y: float
    page: int


@dataclass
class OFDPage:
    """单页的原始行序列（未做噪音清洗）。

    lines[i] 与 line_ys[i] 一一对应：line_ys 为该行首个片段的 Y 坐标
    （行顶近似），供噪音判定计算页眉/页脚区使用。height 为物理页高
    （mm），未知时为 None（分区判定退化为 A4 兜底）。
    """

    index: int
    lines: list[str] = field(default_factory=list)
    line_ys: list[float] = field(default_factory=list)
    height: float | None = None


@dataclass
class ExtractResult:
    """提取结果：清洗后正文 + 统计信息。

    removed_lines 记录被剔除的噪音行原文（页码/页眉/页脚），供上层审计
    "到底删了什么"——噪音剔除最怕误删正文，可审计是硬要求。
    """

    text: str
    pages: int
    is_scanned: bool  # 是否整体无文字层（扫描件/图片型 OFD）
    scanned_pages: int  # 无文字层的页数
    removed_lines: list[str] = field(default_factory=list)
