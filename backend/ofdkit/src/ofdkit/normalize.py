"""文本规范化：公文版式文档常见的字体映射错位修复 + 空白/全半角规范化。

背景（2026-10-15 实测 30 篇四川省政府公报 PDF/OFD）：方正书版系排版软件
生成的文档，其字体 ToUnicode 表把中文标点映射到了错误的 Unicode 区段，
导致解析器如实吐出乱码字符。全语料统计 4300+ 处，直接影响句边界识别
（句号 `。` 变 `ꎮ` 后，句子切分/段落重组全部失去锚点）。

本模块提供：
1. ``FONT_GLYPH_FIXES``：实测上下文逐一核对的映射表（可数据驱动扩充）；
2. ``SUSPECT_RANGES``：中文公文不可能出现的 Unicode 区段（Bamum/Linear B/PUA），
   未命中映射表的可疑字符**不静默丢弃**，保留原文并计数上报；
3. 全角数字/字母半角化（检索友好：`２０２５`→`2025`；中文标点保持全角，
   保公文原貌）；
4. 零宽字符剔除与 NFC 规范化。

该模块为**格式无关**组件：主服务的 PDF/DOCX 清洗链路同样复用，
避免同一套映射表出现双份真相。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 字体映射错位表（实测核对，key=错误码点字符，value=正确字符）
# 注意：码点 > 0xFFFF 必须用 8 位 \U 形式（\u 只取 4 位，会把首字符截断）
# ---------------------------------------------------------------------------
FONT_GLYPH_FIXES: dict[str, str] = {
    "\U0000a3ac": "，",  # Bamum 区错位 → 全角逗号（语料 2597 处）
    "\U0000a3ae": "。",  # Bamum 区错位 → 全角句号（语料 1595 处）
    "\U0000a3bb": "；",  # Bamum 区错位 → 全角分号（语料 124 处）
    "\U001001b0": ".",  # Linear B 区错位 → 英文句点（PM2.5 等，6 处）
    "\U00100170": "·",  # Linear B 区错位 → 间隔号（创投天府·周周见，6 处）
    "\U001003d1": ":",  # Linear B 区错位 → 英文冒号（按 8:2 比例，4 处）
}

# 中文公文不可能出现的 Unicode 区段： Bamum / PUA / Linear B。
# 命中映射表的按表修复；未命中的保留原文 + 计数（新变体发现机制）。
SUSPECT_RANGES: tuple[tuple[int, int], ...] = (
    (0xA000, 0xA8FF),  # Bamum
    (0xE000, 0xF8FF),  # Private Use Area
    (0x100000, 0x1003FF),  # Linear B
)

_FIX_TABLE = str.maketrans(FONT_GLYPH_FIXES)
_SUSPECT_RE = re.compile("[\ua000-\ua8ff\ue000-\uf8ff\U00100000-\U001003ff]")
# 零宽字符：不可见但会进入索引/显示，统一剔除
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u2060]")
# 全角数字/字母 → 半角（检索友好；中文标点不动，保持公文原貌）
_HALFWIDTH_TABLE = str.maketrans(
    {
        cp: chr(cp - 0xFEE0)
        for cp in (
            list(range(0xFF10, 0xFF1A))  # ０-９
            + list(range(0xFF21, 0xFF3B))  # Ａ-Ｚ
            + list(range(0xFF41, 0xFF5B))  # ａ-ｚ
        )
    }
)


def fix_font_glyphs(text: str) -> tuple[str, list[str]]:
    """修复字体映射错位字符。

    Returns:
        (修复后文本, 未命中映射表的可疑区段字符列表)。
        第二个返回值供上层日志告警——发现新变体时按计数扩充映射表，
        而不是静默放过或静默丢弃。
    """
    if not text:
        return text, []
    fixed = text.translate(_FIX_TABLE)
    unmapped = _SUSPECT_RE.findall(fixed)
    return fixed, unmapped


def to_halfwidth_alnum(text: str) -> str:
    """全角数字/字母转半角（中文标点、全角空格保持原样）。

    全角空格是公文首行缩进的语义信号，交由段落重建处理，此处不动。
    """
    return text.translate(_HALFWIDTH_TABLE)


def normalize_text(text: str) -> str:
    """完整规范化：错位修复 → 零宽剔除 → NFC → 全半角统一 → 逐字双写压缩。

    幂等：对已规范化文本重复执行结果不变（供测试断言与重复调用安全）。
    """
    if not text:
        return text
    text, _ = fix_font_glyphs(text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = to_halfwidth_alnum(text)
    text = collapse_char_doubling(text)
    return text


# ---------------------------------------------------------------------------
# 逐字双写压缩（2026-10-15 实测：国家发改委 OFD 红头/页眉特性）
# ---------------------------------------------------------------------------
# 现象：公文排版系统为制造"加粗"观感，把整行文字的每个字写两遍——
#   「国家发展改革委」→「国国家家发发展展和和改改革革委委员员会会」
# 该文本进入正文会伤害检索（BM25 匹配不到"国家发展改革委"）与可读性。
#
# 判据（保守，防误伤正常叠词）：
#   - 行内「相邻相同字符对」占比 ≥ 80%（逐字双写是 100%，叠词只局部重复）；
#   - 压缩后长度 ≥ 4（"哈哈哈""看看"等短叠词不动）。
# 跨页判重能剔多数页眉，但**单页文档**（函/公告）无从判重——本规则兜底。
_DOUBLING_MIN_LEN = 4
_DOUBLING_RATIO = 0.8


def collapse_char_doubling(text: str) -> str:
    """压缩「逐字双写」行（每字连续重复两遍的排版伪粗体）。

    只处理**整行**满足双写特征的情况；行内局部叠词（"研究研究""来来来"）
    不受影响——它们的相邻相同对占比远低于阈值。
    """
    if not text:
        return text
    out_lines: list[str] = []
    for line in text.split("\n"):
        out_lines.append(_collapse_line(line))
    return "\n".join(out_lines)


def _collapse_line(line: str) -> str:
    """单行双写检测与压缩；非双写行原样返回。"""
    n = len(line)
    if n < _DOUBLING_MIN_LEN * 2 or n % 2 != 0:
        return line  # 双写行长度必为偶数且成对出现
    pairs = sum(1 for i in range(0, n, 2) if line[i] == line[i + 1])
    if pairs / (n // 2) < _DOUBLING_RATIO:
        return line
    collapsed = "".join(line[i] for i in range(0, n, 2))
    if len(collapsed) < _DOUBLING_MIN_LEN:
        return line
    return collapsed
