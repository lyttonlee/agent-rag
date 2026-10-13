"""切片：带"上下文前缀"的中文公文切片。

设计要点：
- 上下文前缀 = 标题｜发文机关｜日期，拼接在每个切片前，是提升中文公文
  检索质量性价比最高的一招（让每个切片都携带文档级归属信息）。
- RAG 的精髓在于"怎么切"。不同文种/场景适用的切分策略不同，本模块提供
  四种可切换策略，由接入方在元数据中标明 ``chunk_strategy``：
    * ``paragraph`` 按段落边界切分（默认，最稳妥，尽量不切断语义）
    * ``fixed``     定长滑动窗口（无视边界，适合结构稀疏、追求均匀召回）
    * ``sentence``  按句切分后合并（保证单句完整，适合问答型检索）
    * ``section``   按章节/条款结构切分（公文条款级，保留"第X条/一、"结构）

所有策略都保证：任意切片字符数 <= max_chars，从而不超出 embedding 模型
的上下文上限（Qwen3-Embedding 系列约 4096 token ≈ 数千汉字）。

长短切分（父子块，2026-10-13 P1）
--------------------------------
``chunk_document_dual`` 在**同一套单元提取与重叠机制**上按两个 token 预算
独立切分：
- 小块（small，默认 100–200 token）：检索单元，入库即 embed + tsvector；
- 大块（large，默认 800–2000 token）：生成单元，纯文本不 embed，
  检索命中小块后按需回表注入（见 services/retrieval.py 条件注入）。

token 计量复用成熟库 **chonkie**（MIT，Rust 内核，离线可用，中文密度
≈1 字 1 token，与 Qwen3 tokenizer 实测接近）；chonkie 不可用时退化为
字符计数（1 字符≈1 token 保守近似），不阻断入库。段落/条款/句子等
**公文结构识别为本项目自有领域能力**（通用切分库不具备），继续保留。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

# 支持的切分策略枚举（接入契约的一部分，与前端下拉一致）
ChunkStrategy = Literal["paragraph", "fixed", "sentence", "section"]

# 段落分隔（中文换行 / 空行 / 全角空格）
_SPLIT_RE = re.compile(r"\n\s*\n|\u3000{2,}|(?<=[。！？；])\s*")
# 句子分隔（句末标点或换行）
_SENTENCE_RE = re.compile(r"[。！？；\n]+")
# 章节/条款标题识别（公文常见结构标记）
_SECTION_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零\d]+[章条节篇卷]|"  # 第一章 / 第一条 / 第二节
    r"[一二三四五六七八九十]+、|"                       # 一、 二、
    r"（[一二三四五六七八九十]+）|"                     # （一）（二）
    r"\d+[.、]|"                                       # 1. 2、
    r"[A-Za-z]、|"                                      # A、B、
    r"[0-9]{1,2}、|"                                    # 01、02、
    r"第[一二三四五六七八九十\d]+部分"                  # 第一部分
    r")"
)


@dataclass
class Chunk:
    """单个切片：正文 content（已含前缀）与 prefix（便于回溯）。"""

    content: str
    prefix: str


def build_prefix(title: str, issuer: str | None, publish_date: str | None) -> str:
    """构造文档级上下文前缀：标题｜发文机关｜日期。"""
    parts = [p for p in [title, issuer, publish_date] if p]
    return "｜".join(parts)


def _hard_split(block: str, max_chars: int, overlap: int) -> list[str]:
    """对超长文本块做硬切分，保证每块 <= max_chars，块间重叠 overlap 字符。

    用于无法在语义边界切分的"无边界超长段落"——必须按字数兜底，否则单块
    会超出 embedding 模型的上下文上限。
    """
    pieces: list[str] = []
    start = 0
    n = len(block)
    while start < n:
        end = min(start + max_chars, n)
        pieces.append(block[start:end])
        if end >= n:
            break
        start = end - overlap
    return pieces


def _merge_units(units: list[str], prefix: str, max_chars: int, overlap: int) -> list[Chunk]:
    """将已拆分的最小单元（段落/句子）合并为 <= max_chars 的切片，尾重叠 overlap 字符。"""
    chunks: list[Chunk] = []
    buf = ""
    for unit in units:
        # 单个单元本身已超长：先落库已有 buf，再对该单元硬切为多块
        if len(unit) > max_chars:
            if buf:
                chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
                buf = ""
            for piece in _hard_split(unit, max_chars, overlap):
                chunks.append(Chunk(content=f"{prefix}\n{piece}", prefix=prefix))
            continue
        if len(buf) + len(unit) <= max_chars:
            buf = f"{buf}\n{unit}" if buf else unit
        else:
            if buf:
                chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
            # 重叠：保留尾部若干字符作为上下文衔接
            buf = (buf[-overlap:] + "\n" + unit) if overlap else unit
    if buf:
        chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
    return chunks


# ---------------------------------------------------------------------------
# 各策略的"最小切分单元"提取
# ---------------------------------------------------------------------------

def _paragraph_units(text: str) -> list[str]:
    """段落策略：以段落/句末边界粗分。"""
    return [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]


def _sentence_units(text: str) -> list[str]:
    """句子策略：以句末标点切分为句子，保留标点以维持语义完整。"""
    raw = _SENTENCE_RE.split(text)
    units: list[str] = []
    for seg in raw:
        seg = seg.strip()
        if seg:
            units.append(seg)
    return units


def _section_units(text: str) -> list[str]:
    """章节策略：识别条款标题，将"标题 + 其下正文"作为整体单元。

    若正文过长，后续由 _merge_units 内部的硬切兜底，且前缀会追加章节标题，
    使每个切片都携带所属条款信息。
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    units: list[str] = []
    cur_heading = ""
    cur_body: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if _SECTION_RE.match(stripped):
            # 遇到新标题：先把上一节提交
            if cur_heading or cur_body:
                body = "\n".join(cur_body).strip()
                units.append(f"{cur_heading}\n{body}".strip() if body else cur_heading)
            cur_heading = stripped
            cur_body = []
        else:
            cur_body.append(stripped)
    if cur_heading or cur_body:
        body = "\n".join(cur_body).strip()
        units.append(f"{cur_heading}\n{body}".strip() if body else cur_heading)
    # 若全文未识别出任何章节结构（纯段落），回退为段落单元，避免切成一篇
    return units if units else _paragraph_units(text)


def chunk_document(
    text: str,
    prefix: str,
    strategy: ChunkStrategy = "paragraph",
    max_chars: int = 800,
    overlap: int = 80,
) -> list[Chunk]:
    """按指定策略将正文切分为带前缀的片段。

    Args:
        text: 文档正文（已去除版式噪音）。
        prefix: 文档级上下文前缀（标题｜发文机关｜日期）。
        strategy: 切分策略，见模块顶部说明。
        max_chars: 单切片最大字符数（默认 800）。
        overlap: 相邻切片重叠字符数（默认 80），用于缓解边界信息丢失。

    Returns:
        切片列表（每个切片 content 已含前缀）。
    """
    if not text or not text.strip():
        return []

    if strategy == "fixed":
        # 定长滑动窗口：无视语义边界，纯按字数均分，召回最均匀
        pieces = _hard_split(text, max_chars, overlap)
        return [Chunk(content=f"{prefix}\n{p}", prefix=prefix) for p in pieces]

    if strategy == "sentence":
        units = _sentence_units(text)
    elif strategy == "section":
        units = _section_units(text)
    else:  # paragraph（默认）
        units = _paragraph_units(text)

    return _merge_units(units, prefix, max_chars, overlap)


# ===========================================================================
# 长短切分（父子块）：token 预算 + 双粒度 + 偏移追踪
# ===========================================================================

# chonkie tokenizer 协议（本项目只用三个能力，均为其公开 API）：
#   count_tokens_batch(texts) -> list[int]   批量 token 计数（Rust，快）
#   encode(text) -> list[int] / decode(ids) -> str   精确 token 窗口硬切
Tokenizer = Callable[..., object]


@dataclass
class DualChunk:
    """双粒度切片：content（已含前缀）+ 正文内字符偏移（供父子挂载）。"""

    content: str
    prefix: str
    start: int                       # 正文内起始字符偏移（不含 prefix）
    end: int                         # 正文内结束字符偏移（不含 prefix）
    granularity: str                 # 'small' | 'large'
    parent_index: int | None = None  # 挂载后：所属大块在 large 列表中的下标


@dataclass
class _Unit:
    """带偏移的最小切分单元（段落/句子/条款）。"""

    text: str
    start: int
    end: int
    tokens: int = 0                  # 由批量计数回填


_TOKENIZER: object | None = None
_TOKENIZER_TRIED = False


def get_default_tokenizer():
    """chonkie 默认 tokenizer 进程单例（Rust 内核，构造后复用）。

    chonkie（MIT）是本项目唯一新增的切分依赖，用途严格限定为：
    1. token 精确计量（count_tokens_batch）——配置单位定为 token 的基础；
    2. 超长单元的 token 窗口硬切（encode/decode）——替代字符窗口，与
       embedding 模型的真实 token 边界对齐。

    兜底：chonkie 导入/构造失败（内网安装受阻等）时返回 None，调用方
    退化为字符计数 len()——中文 1 字符≈1 token 的保守近似，切分仍可用
    （实际尺寸略偏小），不阻断入库主链路。
    """
    global _TOKENIZER, _TOKENIZER_TRIED
    if _TOKENIZER_TRIED:
        return _TOKENIZER
    _TOKENIZER_TRIED = True
    try:
        from chonkie import RecursiveChunker

        # 仅借其 tokenizer（不用于递归切分——公文结构识别用自有单元提取）
        _TOKENIZER = RecursiveChunker(chunk_size=1024).tokenizer
    except Exception:  # noqa: BLE001 —— 兜底路径，任何异常都只退化不抛出
        _TOKENIZER = None
    return _TOKENIZER


def _count_tokens(texts: list[str]) -> list[int]:
    """批量 token 计数；无 chonkie 时按字符近似（中文 1 字≈1 token）。"""
    tok = get_default_tokenizer()
    if tok is not None:
        try:
            return list(tok.count_tokens_batch(texts))
        except Exception:  # noqa: BLE001 —— 计数失败降级为字符数，不阻断
            pass
    return [len(t) for t in texts]


def _spans_by_find(text: str, parts: list[str]) -> list[tuple[int, int, str]]:
    """为顺序子串列表定位 (start, end, part)。

    parts 由 split 产生、必为 text 的顺序子串；用锚定 find（从上一段结尾
    继续搜索）逐个定位，O(n) 均摊。找不到的段跳过（防御，正常不发生）。
    """
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for part in parts:
        if not part:
            continue
        idx = text.find(part, pos)
        if idx < 0:
            continue
        spans.append((idx, idx + len(part), part))
        pos = idx + len(part)
    return spans


def _paragraph_units_off(text: str) -> list[_Unit]:
    """段落策略单元（带偏移）：以段落/句末边界粗分。"""
    raw = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    # strip 后可能与前文不连续：用未 strip 的定位 + strip 量修正
    spans = _spans_by_find(text, [p for p in _SPLIT_RE.split(text) if p and p.strip()])
    out: list[_Unit] = []
    for (start, end, part), stripped in zip(spans, raw, strict=True):
        lead = len(part) - len(part.lstrip())
        tail = len(part) - len(part.rstrip())
        out.append(_Unit(text=stripped, start=start + lead, end=end - tail))
    return out


def _sentence_units_off(text: str) -> list[_Unit]:
    """句子策略单元（带偏移）：按句末标点切分，保留标点以维持语义完整。"""
    raw = [seg for seg in _SENTENCE_RE.split(text)]
    spans = _spans_by_find(text, raw)
    out: list[_Unit] = []
    for start, _end, part in spans:
        s = part.strip()
        if not s:
            continue
        lead = len(part) - len(part.lstrip())
        out.append(_Unit(text=s, start=start + lead, end=start + lead + len(s)))
    return out


def _section_units_off(text: str) -> list[_Unit]:
    """章节策略单元（带偏移）：'标题 + 其下正文'作为整体。

    逐行扫描并记录行偏移；新标题行开启新单元，单元 span 覆盖从标题行
    起点到本节最后一行终点。未识别出章节结构时回退段落单元。
    """
    units: list[_Unit] = []
    line_spans: list[tuple[int, int, str]] = []
    pos = 0
    for ln in text.splitlines(keepends=True):
        stripped = ln.strip()
        line_spans.append((pos, pos + len(ln), stripped))
        pos += len(ln)
    cur_start: int | None = None
    cur_end = 0
    cur_lines: list[str] = []
    for start, end, stripped in line_spans:
        if not stripped:
            if cur_start is not None:
                cur_end = end
            continue
        if _SECTION_RE.match(stripped):
            if cur_lines:
                units.append(
                    _Unit(text="\n".join(cur_lines), start=cur_start or 0, end=cur_end)
                )
            cur_start, cur_end, cur_lines = start, end, [stripped]
        else:
            if cur_start is None:
                cur_start = start
            cur_lines.append(stripped)
            cur_end = end
    if cur_lines:
        units.append(_Unit(text="\n".join(cur_lines), start=cur_start or 0, end=cur_end))
    return units if units else _paragraph_units_off(text)


def _units_with_offsets(text: str, strategy: ChunkStrategy) -> list[_Unit]:
    """按策略提取带偏移的单元列表（四种策略与 chunk_document 一致）。"""
    if strategy == "sentence":
        return _sentence_units_off(text)
    if strategy == "section":
        return _section_units_off(text)
    if strategy == "fixed":
        # fixed 策略无语义单元：以全文为单一单元，交给 _merge 的硬切窗口处理
        return [_Unit(text=text, start=0, end=len(text))] if text.strip() else []
    return _paragraph_units_off(text)


def _token_windows(u: _Unit, max_tokens: int, overlap_tokens: int) -> list[tuple[str, int, int]]:
    """对超长单元做 token 窗口硬切（替代字符窗口，与 embedding token 边界对齐）。

    返回 [(piece_text, start, end), ...]。偏移按 token→字符密度**比例估算**：
    偏移仅用于父子挂载（启发式取最大重叠），比例估算天然单调、有界
    （末窗终点收敛到单元末尾）；若用锚定 find 精确定位，重叠窗口会导致
    下一窗起点早于上一窗终点而搜不到，反而产生非单调偏移（实测踩坑）。
    piece 文本取自 decode 原文（不按偏移回切），杜绝文本损坏。
    """
    tok = get_default_tokenizer()
    if tok is None:  # 无 chonkie：退化为字符窗口（比例近似 1 字符≈1 token）
        pieces = _hard_split(u.text, max_tokens, overlap_tokens)
        out: list[tuple[str, int, int]] = []
        char_pos = 0
        for p in pieces:
            idx = u.text.find(p, char_pos)
            if idx < 0:
                idx = char_pos
            out.append((p, u.start + idx, u.start + idx + len(p)))
            char_pos = idx + len(p)
        return out

    ids = tok.encode(u.text)
    n = len(ids)
    if n == 0:
        return []
    ratio = len(u.text) / n  # 字符/token 密度（全文标定，窗口间一致）
    step = max(max_tokens - overlap_tokens, 1) if overlap_tokens > 0 else max_tokens
    out = []
    start_tok = 0
    while start_tok < n:
        end_tok = min(start_tok + max_tokens, n)
        piece = tok.decode(ids[start_tok:end_tok])
        p_start = u.start + int(start_tok * ratio)
        p_end = u.start + int(end_tok * ratio)
        out.append((piece, p_start, max(p_end, p_start + 1)))
        if end_tok >= n:
            break
        start_tok += step
    return out


def _merge_budgeted(
    units: list[_Unit],
    prefix: str,
    max_tokens: int,
    overhead_tokens: int,
    overlap_tokens: int,
    granularity: str,
) -> list[DualChunk]:
    """把带偏移单元合并为总 token <= max_tokens 的切片，尾部按 token 预算回填重叠。

    与旧 ``_merge_units`` 同款缓冲逻辑，差异在：
    1. 计量单位为 token（配置中心驱动），且**预算覆盖完整切片**
       （正文 + 上下文前缀 + 换行分隔）——前缀是切片的一部分（要 embed、
       要注入生成上下文），“切片 token 上限”必须是用户看到的那个数；
    2. 重叠按单元尾部回填（保持段落/条款完整，不切断）；
    3. 输出携带字符偏移（供父子挂载）。

    Args:
        overhead_tokens: 前缀 + 换行的 token 开销（每切片固定成本）。
        overlap_tokens: 尾重叠 token 预算（夹取到单元预算内）。
    """
    unit_budget = max(max_tokens - overhead_tokens, 1)
    overlap_tokens = min(overlap_tokens, unit_budget - 1) if unit_budget > 1 else 0
    chunks: list[DualChunk] = []
    buf: list[_Unit] = []
    buf_tokens = 0

    def _flush() -> list[_Unit]:
        nonlocal buf, buf_tokens
        if buf:
            start, end = buf[0].start, buf[-1].end
            body = "\n".join(u.text for u in buf)
            chunks.append(
                DualChunk(
                    content=f"{prefix}\n{body}",
                    prefix=prefix,
                    start=start,
                    end=end,
                    granularity=granularity,
                )
            )
        flushed, buf, buf_tokens = buf, [], 0
        return flushed

    for u in units:
        if u.tokens > unit_budget:
            # 单元本身超预算：先落库已有缓冲，再对该单元做 token 窗口硬切
            _flush()
            for piece, p_start, p_end in _token_windows(u, unit_budget, overlap_tokens):
                chunks.append(
                    DualChunk(
                        content=f"{prefix}\n{piece}",
                        prefix=prefix,
                        start=p_start,
                        end=p_end,
                        granularity=granularity,
                    )
                )
            continue
        # 入缓冲成本：前缀开销 + 换行分隔（≈1 token）+ 单元本身
        # （前缀是每切片固定成本，任何一次追加都要把它计入总账）
        sep = 1 if buf else 0
        if overhead_tokens + buf_tokens + sep + u.tokens <= max_tokens:
            buf.append(u)
            buf_tokens += sep + u.tokens
            continue
        flushed = _flush()
        # 尾重叠：从上一缓冲尾部回填单元，直到重叠 token 预算用满；
        # 回填后必须仍能容纳新单元，否则放弃回填（保新单元优先）
        if overlap_tokens > 0:
            back: list[_Unit] = []
            used = 0
            for prev in reversed(flushed):
                cost = prev.tokens + 1
                if used + cost > overlap_tokens:
                    break
                back.insert(0, prev)
                used += cost
            if overhead_tokens + used + 1 + u.tokens <= max_tokens:
                buf, buf_tokens = back, used
        buf.append(u)
        buf_tokens += u.tokens
    _flush()
    return chunks


def chunk_document_dual(
    text: str,
    prefix: str,
    strategy: ChunkStrategy = "paragraph",
    small_max_tokens: int = 150,
    small_overlap_tokens: int = 20,
    large_max_tokens: int = 1200,
    large_overlap_tokens: int = 100,
) -> tuple[list[DualChunk], list[DualChunk]]:
    """双粒度切分：返回 (小块列表, 大块列表)，均带正文偏移。

    Args:
        text: 文档正文（已去除版式噪音）。
        prefix: 文档级上下文前缀（标题｜发文机关｜日期）。
        strategy: 切分策略（与 chunk_document 四种一致，公文章节/段落识别保留）。
        small_max_tokens: 小块 token 上限（配置中心 retrieval.small_max_tokens，
            用户需求区间 100–200；**含前缀的完整切片预算**）。
        small_overlap_tokens: 小块尾重叠 token 预算。
        large_max_tokens: 大块 token 上限（配置中心 retrieval.large_max_tokens，
            用户需求区间 800–2000；同样含前缀）。
        large_overlap_tokens: 大块尾重叠 token 预算。

    Returns:
        (small, large)：两级粒度**独立切分**（边界天然错位，各自保留
        段落/条款边界与尾重叠）；父子挂载由 ``mount_parents`` 按偏移
        重叠比例完成，不强行对齐两级边界（对齐会牺牲边界质量）。

    设计说明：小块与大块复用同一套单元提取与 token 计数（一次批量计数
    两级都用），大块不 embed——检索走小块、生成按需回表取大块，
    这是 small-to-large 模式在单库 PG 上的落地形态。
    """
    if not text or not text.strip():
        return [], []

    units = _units_with_offsets(text, strategy)
    if not units:
        return [], []

    # 批量 token 计数一次，两级粒度复用（Rust 批量接口，避免逐单元调用开销）
    counts = _count_tokens([u.text for u in units])
    for u, c in zip(units, counts, strict=True):
        u.tokens = max(c, 1)

    # 前缀开销（前缀 + 换行分隔）：每切片的固定 token 成本，计入预算
    overhead = max(_count_tokens([f"{prefix}\n"])[0], 1)

    small = _merge_budgeted(
        units, prefix, small_max_tokens, overhead, small_overlap_tokens, "small"
    )
    large = _merge_budgeted(
        units, prefix, large_max_tokens, overhead, large_overlap_tokens, "large"
    )
    return small, large


def mount_parents(smalls: list[DualChunk], larges: list[DualChunk]) -> int:
    """按正文偏移重叠比例为每个小块挂父块（写入 small.parent_index）。

    评分 = 重叠字符数 / 小块长度（小块被父块覆盖的比例，0~1）；
    零重叠的极端边界 case 退化为"中心距最近"父块，保证 parent_index
    尽量非空。返回成功挂载数量。

    两级独立切分所以边界错位是常态——挂载是启发式，追求"父块覆盖
    小块主体"而非严丝合缝。
    """
    if not larges:
        return 0
    mounted = 0
    for s in smalls:
        s_len = max(s.end - s.start, 1)
        best_idx: int | None = None
        best_score = 0.0
        for i, lg in enumerate(larges):
            ov = min(s.end, lg.end) - max(s.start, lg.start)
            if ov > 0:
                score = ov / s_len
                if score > best_score:
                    best_idx, best_score = i, score
        if best_idx is None:
            # 零重叠兜底：中心距最近
            s_mid = (s.start + s.end) / 2
            best_idx = min(
                range(len(larges)), key=lambda i: abs((larges[i].start + larges[i].end) / 2 - s_mid)
            )
        s.parent_index = best_idx
        mounted += 1
    return mounted
