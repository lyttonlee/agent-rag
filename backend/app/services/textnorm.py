"""文本规范化门面：主服务内部的清洗入口。

实现收敛于独立库 ofdkit（``ofdkit.normalize``）——OFD 库、PDF、DOCX、
TXT 四条解析链路共用同一套「字体映射错位修复 + 全半角/零宽规范化」，
避免双份真相。本模块只做门面 re-export，并附主服务侧的使用约定：

- 乱码码点映射表以 ofdkit 的 ``FONT_GLYPH_FIXES`` 为准（30 篇政府公报
  语料实测，上下文逐一核对）；发现新变体时扩充 ofdkit 的表并升库版本；
- 全角数字/字母半角化利于 BM25 检索（`２０２５`→`2025`），中文标点保持
  全角保公文原貌；全角空格是首行缩进信号，交由段落重建处理，不在此处动。
"""

from __future__ import annotations

from ofdkit.normalize import (  # noqa: F401 —— 门面 re-export，供主服务统一引用
    FONT_GLYPH_FIXES,
    SUSPECT_RANGES,
    fix_font_glyphs,
    normalize_text,
    to_halfwidth_alnum,
)

__all__ = [
    "FONT_GLYPH_FIXES",
    "SUSPECT_RANGES",
    "fix_font_glyphs",
    "normalize_text",
    "to_halfwidth_alnum",
]
