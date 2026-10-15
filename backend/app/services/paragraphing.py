"""段落重建：物理行序列 → 自然段序列。

PDF/OFD 提取出的文本是「物理行」序列——一行只是一个排版行，不等于自然段：
公文正文软换行（一行约 28-32 字）、跨页 continuation、标题行独占一行。
若直接把物理行当段落，切分会把一个自然段切得粉碎；若把全文当一段，
又失去结构。本模块按多信号规则把物理行重组为自然段：

1. **空行** → 段落边界（DOCX/TXT 显式分段，最高优先级）；
2. **标题行**（第X章/一、/（一）/1. 等条款模式，且行长短）→ 强制新段，
   且标题行**独立成段**（其后正文新起一段——条款标题是公文的天然结构点）；
3. **日期行 / 发文字号行**（`2025年10月30日` / `川府函〔2025〕307号` 落款形态）
   → 同样独立成段；
4. **括号/引号跨行**（上行以 （《【“‘ 结尾，或本行以 ）》】”’ 开头）→ 合并；
5. **冒号强边界**：上行以 `:`/`：` 结尾 → 换段（主送机关/引导语的可靠信号，
   优先于满行判断——引用跨行已由开引号规则兜住）；
6. **软换行判定**（PDF 核心信号，仅当全文行长分布高度一致时启用——
   即"定宽折行"排版）：
   - 上行是「满行」（长度 ≥ 折行阈值且 ≥16 字）→ 判为软换行，合并；
   - 否则上行以句末标点（。！？；）结尾 → 段落结束，新段；
7. 其余 → 合并（软换行）。

为什么用「满行」而不是只看句末标点：公文一段含多个句子，软换行点
可能恰好落在句号后（满行 + 句号），只看标点会把一段切成两段；满行信号
（该行仍被版心折行）比标点更可靠。两者结合：满行必续，短行+标点必断。

所有段落携带在原文中的字符偏移 [start, end)，供父子块挂载使用。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

# 条款/章节标题行模式（与 chunker 的 section 策略共用同一识别标准）
# 注意：PDF 提取常把全角括号转半角（`(一)`），两种形态都要覆盖
SECTION_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零\d]+[章条节篇卷部]|"  # 第一章 / 第一条 / 第一部分
    r"[一二三四五六七八九十]+、|"  # 一、 二、
    r"（[一二三四五六七八九十]+）|"  # （一）（二）全角
    r"\([一二三四五六七八九十]+\)|"  # (一)(二)半角（PDF 常见）
    r"\([0-9]{1,2}\)|"  # (1) (2)
    r"\d+[.、]|"  # 1. 2、
    r"[A-Za-z]、|"  # A、B、
    r"[0-9]{1,2}、|"  # 01、02、
    r"附件[一二三四五六七八九十\d]*[:：]?"  # 附件：/附件1：
    r")"
)
# 标题行长度上限：超过该长度的「标题模式」行更可能是正文以数字开头
_HEADING_MAX_LEN = 50
# 发文字号行：川府函〔2025〕307号（全角/半角括号与数字都要兼容）
_DOC_NO_LINE_RE = re.compile(r"^[\u4e00-\u9fa5]{1,8}[〔\[][０-９\d]{4}[〕\]]\s*[０-９\d]+\s*号?$")
# 落款日期行：2025年10月30日（数字形态）
_DATE_LINE_RE = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$")
# 句末标点（段落结束信号）
_SENT_END = "。！？；"
# 开/闭括号与引号（跨行合并信号）
_OPEN_BRACKETS = "（《【“‘([{<"
_CLOSE_BRACKETS = "）》】”’)]}>"
# 满行阈值系数：全文最长行 × 该系数 = 折行宽度估计
_FULL_LINE_RATIO = 0.85
# 满行最小长度：短于此的行不可能是"被版心折行"的整行（防短文本误判）
_MIN_FULL_LINE = 16
# 行长分布一致性阈值：stdev/mean ≤ 该值才认定为「定宽折行」排版（PDF 软换行），
# 否则（DOCX/TXT 天然分段、行长参差）不启用满行规则，只按标点/标题分段
_UNIFORM_RATIO = 0.30


@dataclass
class Paragraph:
    """自然段：text（合并后正文，不含换行）+ 原文偏移 [start, end)。"""

    text: str
    start: int
    end: int


def looks_like_heading(line: str) -> bool:
    """是否条款/章节标题行（模式命中且足够短）。"""
    s = line.strip()
    return bool(s) and len(s) <= _HEADING_MAX_LEN and bool(SECTION_HEADING_RE.match(s))


def is_standalone_line(line: str) -> bool:
    """是否"孤立版式行"：标题/日期/文号——这些行必须独立成段。"""
    s = line.strip()
    return looks_like_heading(s) or bool(_DATE_LINE_RE.match(s)) or bool(_DOC_NO_LINE_RE.match(s))


def _join_lines(lines: list[str]) -> str:
    r"""软换行拼接：中文直接相接；半角单词断裂（行尾行首均为 ASCII 字母
    数字）补一个空格，避免 ``inter national`` 粘成一个词。"""
    if not lines:
        return ""
    out = lines[0]
    for nxt in lines[1:]:
        if out and nxt and out[-1].isascii() and out[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum():
            out += " "
        out += nxt
    return out


def _is_soft_wrapped_layout(line_lens: list[int]) -> bool:
    """全文行长分布是否「定宽折行」（PDF 软换行排版）。

    判据：行长标准差 ≤ 均值 30%——软换行文本的行长紧密聚集在折行宽度
    附近（段尾短行是少数）；DOCX/TXT 天然分段文本行长方差大，不启用
    满行规则（避免把两个完整段落误粘一段）。
    """
    if len(line_lens) < 4:
        return True  # 行数太少无法估计分布：默认启用（PDF 是主要场景）
    mean = statistics.fmean(line_lens)
    if mean <= 0:
        return False
    return statistics.pstdev(line_lens) <= _UNIFORM_RATIO * mean


def rebuild_paragraphs(
    text: str,
    *,
    enabled: bool = True,
    full_line_ratio: float = _FULL_LINE_RATIO,
) -> list[Paragraph]:
    """把物理行序列重组为自然段序列。

    Args:
        text: 含物理行的正文（\\n 分隔）。
        enabled: False 时退化为「仅按空行分段」的保守模式（不做满行/缩进
            推断），供配置中心 ``chunk_paragraph_rebuild`` 关闭时使用。
        full_line_ratio: 满行阈值系数（最长行 × 该值 = 折行宽度）。

    Returns:
        自然段列表（含原文偏移）。空文本返回 []。
    """
    if not text or not text.strip():
        return []

    # 1) 物理行 + 偏移（keepends 保留换行符，偏移才能连续）；
    #    空行也记录——它是显式分段信号（DOCX 段落间以空行连接）。
    raw_lines = text.splitlines(keepends=True)
    items: list[tuple[str, int, int, str]] = []  # (kind, start, end, stripped)
    pos = 0
    for ln in raw_lines:
        s = ln.strip()
        if s:
            lead = len(ln) - len(ln.lstrip())
            items.append(("text", pos + lead, pos + len(ln.rstrip()), s))
        else:
            items.append(("blank", pos, pos + len(ln), ""))
        pos += len(ln)

    text_items = [it for it in items if it[0] == "text"]
    if not text_items:
        return []

    if not enabled:
        # 保守模式：仅空行分段（连续非空行并成一段，不做任何形态推断）
        paras: list[Paragraph] = []
        prev_blank = True  # 文首视作边界
        for kind, start, end, s in items:
            if kind == "blank":
                prev_blank = True
                continue
            if prev_blank or not paras:
                paras.append(Paragraph(text=s, start=start, end=end))
            else:
                prev = paras[-1]
                prev.text = _join_lines([prev.text, s])
                prev.end = end
            prev_blank = False
        return paras

    # 2) 折行宽度估计（仅定宽折行排版启用满行规则）
    line_lens = [len(s) for _, _, _, s in text_items]
    max_len = max(line_lens)
    wrap_threshold = max(int(max_len * full_line_ratio), _MIN_FULL_LINE)
    soft_layout = _is_soft_wrapped_layout(line_lens)

    # 3) 逐行判定新段/续段
    paras: list[Paragraph] = []
    buf: list[tuple[int, int, str]] = []  # (start, end, stripped)

    def _flush() -> None:
        nonlocal buf
        if buf:
            paras.append(
                Paragraph(
                    text=_join_lines([s for _, _, s in buf]),
                    start=buf[0][0],
                    end=buf[-1][1],
                )
            )
        buf = []

    for kind, start, end, s in items:
        if kind == "blank":
            _flush()  # 空行：显式段落边界（最高优先级）
            continue
        if not buf:
            buf.append((start, end, s))
            continue
        prev = buf[-1][2]
        new_para = False

        if is_standalone_line(s):
            new_para = True  # 规则 2/3：标题/日期/文号行强制新段
        elif s[0] in _OPEN_BRACKETS or prev[-1] in _CLOSE_BRACKETS:
            new_para = False  # 规则 4：括号/引号跨行必须合并
        elif prev[-1] in _OPEN_BRACKETS:
            new_para = False  # 规则 4：上行以开括号/引号结尾，必然未完
        elif is_standalone_line(prev):
            new_para = True  # 规则 2/3b：孤立版式行独立成段，其后正文新起
        elif prev[-1] in (":", "："):
            new_para = True  # 规则 5：冒号强边界（主送机关/引导语）
        elif soft_layout and len(prev) >= wrap_threshold:
            new_para = False  # 规则 6a：满行 = 软换行，续段
        elif prev[-1] in _SENT_END:
            new_para = True  # 规则 6b：短行 + 句末标点 → 段落结束
        # 其余（短行无句末标点，如标题跨行/落款）→ 续段

        if new_para:
            _flush()
        buf.append((start, end, s))
    _flush()
    return paras
