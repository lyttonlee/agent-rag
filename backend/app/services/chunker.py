"""切片：带"上下文前缀"的中文公文切片（段落优先，2026-10-15 V2.0 重构）。

设计要点：
- 上下文前缀 = 标题｜发文机关｜日期，拼接在每个切片前，是提升中文公文
  检索质量性价比最高的一招（让每个切片都携带文档级归属信息）。
- **段落优先（V2.0 核心变化）**：默认 ``paragraph`` 策略的切分单元是
  **自然段**（经 ``services/paragraphing.py`` 从物理行重组），段内不
  切断；仅当单个段落超出 token 预算时才逐级降级切分（见下）。旧实现
  的"段落策略"实际在句末标点后切分（单元是句子），一个 300 token 的
  小块可跨 2-3 个自然段，段落边界被无视——本次重构修正该问题。
- 逐级降级（单段超预算时）：
    1. 句边界软切（。！？；整句归整）
    2. 单句超预算 → 子句边界（，、：整子句归整）
    3. 单子句超预算 → token 窗口硬切（与 embedding token 边界对齐）
  每一级都保证"不断的不切，必须切时切在最接近的语义边界"。
- 四种策略由接入方在元数据标明 ``chunk_strategy``：
    * ``paragraph`` 按自然段切分（默认；段内不切，跨段合并至预算）
    * ``fixed``     定长滑动窗口（无视边界，适合结构稀疏、追求均匀召回）
    * ``sentence``  按句切分后合并（保证单句完整，适合问答型检索）
    * ``section``   按章节/条款结构切分（条款标题 + 其下正文为整体）

长短切分（父子块）
--------------------------------
``chunk_document_dual`` 在**同一套单元提取与重叠机制**上按两个 token 预算
独立切分：
- 小块（small，默认 300 token）：检索单元，入库即 embed + tsvector；
- 大块（large，默认 2500 token）：生成单元，纯文本不 embed，
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

from app.services.paragraphing import (
    is_standalone_line,
    rebuild_paragraphs,
)

# 支持的切分策略枚举（接入契约的一部分，与前端下拉一致）
ChunkStrategy = Literal["paragraph", "fixed", "sentence", "section"]

# 句子分隔（句末标点或换行）
_SENTENCE_RE = re.compile(r"[。！？；\n]+")
# 子句分隔（逗号/顿号/冒号——句子内部的一级语义边界）
_CLAUSE_RE = re.compile(r"[，、：]+")


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

    用于无法在语义边界切分的"无边界超长文本"——必须按字数兜底，否则单块
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
        # 单个单元本身已超长：先落库已有 buf，再对该单元逐级降级切分
        if len(unit) > max_chars:
            if buf:
                chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
                buf = ""
            for piece in _split_over_long_chars(unit, max_chars, overlap):
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


def _sentence_units(text: str) -> list[str]:
    """句子策略：以句末标点切分为句子，**保留标点**以维持语义完整。

    旧实现用 split 丢掉了句末标点（单元以正文结尾），既损失语义又让
    「断点在句边界」的校验失效；改用带捕获的 scan，标点归属前句。
    """
    return [seg for _s, _e, seg in _sentence_spans(text)]


def _split_over_long_chars(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长单元的字符口径逐级降级切分（单粒度存量路径用）。

    与 token 口径的 ``_soft_windows`` 同款思想：句边界 → 子句边界 →
    硬切，保证不断的不切。
    """
    # 1) 句边界打包
    sent_spans = _sentence_spans(text)
    if sent_spans:
        pieces: list[str] = []
        buf, buf_len = "", 0
        for _s, _e, seg in sent_spans:
            if len(seg) > max_chars:
                if buf:
                    pieces.append(buf)
                buf, buf_len = "", 0
                pieces.extend(_split_over_long_chars(seg, max_chars, 0))  # 递归降级
                continue
            if buf_len + len(seg) <= max_chars:
                buf += seg
                buf_len += len(seg)
            else:
                if buf:
                    pieces.append(buf)
                buf, buf_len = seg, len(seg)
        if buf:
            pieces.append(buf)
        return pieces
    # 2) 子句边界打包
    clause_spans = _clause_spans(text)
    if clause_spans:
        pieces = []
        buf, buf_len = "", 0
        for _s, _e, seg in clause_spans:
            if len(seg) > max_chars:
                if buf:
                    pieces.append(buf)
                buf, buf_len = "", 0
                pieces.extend(_hard_split(seg, max_chars, overlap))
                continue
            if buf_len + len(seg) <= max_chars:
                buf += seg
                buf_len += len(seg)
            else:
                if buf:
                    pieces.append(buf)
                buf, buf_len = seg, len(seg)
        if buf:
            pieces.append(buf)
        return pieces
    # 3) 无语义边界：硬切兜底
    return _hard_split(text, max_chars, overlap)


def chunk_document(
    text: str,
    prefix: str,
    strategy: ChunkStrategy = "paragraph",
    max_chars: int = 800,
    overlap: int = 80,
    *,
    rebuild_paragraphs_flag: bool = True,
) -> list[Chunk]:
    """按指定策略将正文切分为带前缀的片段（单粒度存量路径）。

    Args:
        text: 文档正文（已去除版式噪音）。
        prefix: 文档级上下文前缀（标题｜发文机关｜日期）。
        strategy: 切分策略，见模块顶部说明。
        max_chars: 单切片最大字符数（默认 800）。
        overlap: 相邻切片重叠字符数（默认 80），用于缓解边界信息丢失。
        rebuild_paragraphs_flag: 是否把物理行重组为自然段（默认开；
            配置中心 ``chunk_paragraph_rebuild`` 可关）。

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
        units = _section_units(text, rebuild=rebuild_paragraphs_flag)
    else:  # paragraph（默认）
        units = _paragraph_units(text, rebuild=rebuild_paragraphs_flag)

    return _merge_units(units, prefix, max_chars, overlap)


def _paragraph_units(text: str, *, rebuild: bool = True) -> list[str]:
    """段落策略单元：自然段（物理行重组）；rebuild 关时仅按空行分段。"""
    return [p.text for p in rebuild_paragraphs(text, enabled=rebuild)]


def _section_units(text: str, *, rebuild: bool = True) -> list[str]:
    """章节策略单元：'条款标题段 + 其下正文段'作为整体。

    V2.0 起基于自然段（而非物理行）判定：标题模式命中的段落开启新单元，
    后续正文段并入该单元，直至下一标题段——条款标题与其正文不分离，
    检索时"第X条 + 内容"整体命中。未识别出章节结构时回退段落单元。
    """
    paras = rebuild_paragraphs(text, enabled=rebuild)
    units: list[str] = []
    for p in paras:
        if not units or is_standalone_line(p.text):
            units.append(p.text)  # 标题/日期/文号段开启新单元（条款级召回）
        else:
            units[-1] = f"{units[-1]}\n{p.text}"  # 正文并入当前条款单元
    return units if units else [p.text for p in paras]


def _is_head_text(t: str) -> bool:
    """单元文本是否为条款标题/日期行（章节策略的成组判断）。"""
    return is_standalone_line(t)


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
    start: int  # 正文内起始字符偏移（不含 prefix）
    end: int  # 正文内结束字符偏移（不含 prefix）
    granularity: str  # 'small' | 'large'
    parent_index: int | None = None  # 挂载后：所属大块在 large 列表中的下标


@dataclass
class _Unit:
    """带偏移的最小切分单元（自然段/句子/条款）。"""

    text: str
    start: int
    end: int
    tokens: int = 0  # 由批量计数回填


_TO_TOKENIZER: object | None = None
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


def _paragraph_units_off(text: str, *, rebuild: bool = True) -> list[_Unit]:
    """段落策略单元（带偏移）：自然段（物理行重组，V2.0 起）。"""
    return [_Unit(text=p.text, start=p.start, end=p.end) for p in rebuild_paragraphs(text, enabled=rebuild)]


def _sentence_units_off(text: str) -> list[_Unit]:
    """句子策略单元（带偏移）：按句末标点切分，**保留标点**归属前句。"""
    out: list[_Unit] = []
    for start, end, seg in _sentence_spans(text):
        s = seg.strip()
        if not s:
            continue
        lead = len(seg) - len(seg.lstrip())
        out.append(_Unit(text=s, start=start + lead, end=end))
    return out


def _section_units_off(text: str, *, rebuild: bool = True) -> list[_Unit]:
    """章节策略单元（带偏移）：基于自然段的'标题段 + 正文段'整体。

    与旧行扫描版的差异：正文段保持多行完整（不会被行边界切碎），
    条款标题段开启新单元，后续正文段并入，偏移跨整个条款。
    """
    units: list[_Unit] = []
    for p in rebuild_paragraphs(text, enabled=rebuild):
        if not units or is_standalone_line(p.text):
            units.append(_Unit(text=p.text, start=p.start, end=p.end))
        else:
            prev = units[-1]
            units[-1] = _Unit(text=f"{prev.text}\n{p.text}", start=prev.start, end=p.end)
    return units if units else _paragraph_units_off(text, rebuild=rebuild)


def _units_with_offsets(text: str, strategy: ChunkStrategy, *, rebuild: bool = True) -> list[_Unit]:
    """按策略提取带偏移的单元列表（四种策略与 chunk_document 一致）。"""
    if strategy == "sentence":
        return _sentence_units_off(text)
    if strategy == "section":
        return _section_units_off(text, rebuild=rebuild)
    if strategy == "fixed":
        # fixed 策略无语义单元：以全文为单一单元，交给 _merge 的硬切窗口处理
        return [_Unit(text=text, start=0, end=len(text))] if text.strip() else []
    return _paragraph_units_off(text, rebuild=rebuild)


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


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    """按句末标点（。！？；及换行）切句并定位 (start, end, seg)，保留标点不断句。

    用于超大单元的「软切分」：优先在句边界成块，避免把一句话从中间切断
    （token 窗口硬切会切碎语义）。标点归属前句；空段跳过。
    """
    spans: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[^。！？；\n]*(?:[。！？；\n]|$)", text):
        s, e = m.start(), m.end()
        seg = text[s:e]
        if seg and seg.strip():
            spans.append((s, e, seg))
    return spans


def _clause_spans(text: str) -> list[tuple[int, int, str]]:
    """按子句标点（，、：）切分并定位 (start, end, seg)，保留标点归属前句。

    二级语义边界：单句本身超预算时，在子句边界成块，仍不切断子句。
    """
    spans: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[^，、：]*(?:[，、：]|$)", text):
        s, e = m.start(), m.end()
        seg = text[s:e]
        if seg and seg.strip():
            spans.append((s, e, seg))
    return spans


def _pack_spans(
    spans: list[tuple[int, int, str]],
    counts: list[int],
    base: int,
    max_tokens: int,
) -> list[tuple[str, int, int]]:
    """把带计数的片段序列贪心打包为 <= max_tokens 的块（保留原顺序）。

    单片段超预算时由调用方先行处理（降级或硬切），此处只做整段归整。
    返回 [(piece_text, abs_start, abs_end)]。
    """
    out: list[tuple[str, int, int]] = []
    buf, buf_s, buf_e, buf_tok = "", 0, 0, 0
    for (rs, re_, seg), c in zip(spans, counts, strict=True):
        if buf and buf_tok + c > max_tokens:
            out.append((buf, buf_s, buf_e))
            buf, buf_tok = "", 0
        if not buf:
            buf_s = base + rs
        buf += seg
        buf_e = base + re_
        buf_tok += c
    if buf:
        out.append((buf, buf_s, buf_e))
    return out


def _soft_windows(u: _Unit, max_tokens: int, overlap_tokens: int) -> list[tuple[str, int, int]]:
    """超大单元软切分（四级降级的第 1 级）：句边界整句归整打包。

    与 ``_token_windows``（任意 token 处硬切）的差异：
    1. 整句归整，显著降低「半句话被腰斩」的完整性损失（切分完整性二期核心）；
    2. 块间不重复（无滑动重叠），避免同句跨块冗余；仅最终兜底硬切仍保留
       token 窗口重叠；
    3. 偏移取句子精确 span（非比例估算），父子挂载更准。

    单句本身超预算时，该句交给 ``_split_unit_graceful(_seen_sentence=True)``
    走子句边界/硬切降级——即降级链：句边界 → 子句边界 → token 硬切。

    Returns:
        [(piece_text, abs_start, abs_end), ...]，偏移为正文内绝对位置。
    """
    sent_spans = _sentence_spans(u.text)
    if not sent_spans:
        # 无句边界（如纯数字串）：直接走子句/硬切降级
        return _clause_or_token_windows(u, max_tokens, overlap_tokens)
    counts = _count_tokens([seg for _, _, seg in sent_spans])
    if all(c <= max_tokens for c in counts):
        # 全部句子放得下：按句边界贪心打包（最常见路径）
        return _pack_spans(sent_spans, counts, u.start, max_tokens)
    # 存在超预算句：逐句分发——放得下的按句打包，超预算的递归降级
    return _distribute_spans(sent_spans, counts, u, max_tokens, overlap_tokens)


def _distribute_spans(
    sent_spans: list[tuple[int, int, str]],
    counts: list[int],
    u: _Unit,
    max_tokens: int,
    overlap_tokens: int,
) -> list[tuple[str, int, int]]:
    """句级分发：放得下的句子按句边界打包；超预算的句子递归降级。"""
    out: list[tuple[str, int, int]] = []
    normal: list[tuple[int, int, str]] = []
    normal_counts: list[int] = []
    for (rs, re_, seg), c in zip(sent_spans, counts, strict=True):
        if c > max_tokens:
            if normal:
                out.extend(_pack_spans(normal, normal_counts, u.start, max_tokens))
                normal, normal_counts = [], []
            sub = _Unit(text=seg, start=u.start + rs, end=u.start + re_)
            out.extend(_split_unit_graceful(sub, max_tokens, overlap_tokens, _seen_sentence=True))
        else:
            normal.append((rs, re_, seg))
            normal_counts.append(c)
    if normal:
        out.extend(_pack_spans(normal, normal_counts, u.start, max_tokens))
    return out


def _clause_or_token_windows(u: _Unit, max_tokens: int, overlap_tokens: int) -> list[tuple[str, int, int]]:
    """无句边界单元的子句/硬切降级。"""
    clause_spans = _clause_spans(u.text)
    if not clause_spans:
        return _token_windows(u, max_tokens, overlap_tokens)
    counts = _count_tokens([seg for _, _, seg in clause_spans])
    if all(c <= max_tokens for c in counts):
        return _pack_spans(clause_spans, counts, u.start, max_tokens)
    out: list[tuple[str, int, int]] = []
    normal: list[tuple[int, int, str]] = []
    normal_counts: list[int] = []
    for (rs, re_, seg), c in zip(clause_spans, counts, strict=True):
        if c > max_tokens:
            if normal:
                out.extend(_pack_spans(normal, normal_counts, u.start, max_tokens))
                normal, normal_counts = [], []
            sub = _Unit(text=seg, start=u.start + rs, end=u.start + re_)
            out.extend(_token_windows(sub, max_tokens, overlap_tokens))
        else:
            normal.append((rs, re_, seg))
            normal_counts.append(c)
    if normal:
        out.extend(_pack_spans(normal, normal_counts, u.start, max_tokens))
    return out


def _split_unit_graceful(
    u: _Unit,
    max_tokens: int,
    overlap_tokens: int,
    *,
    _seen_sentence: bool = False,
) -> list[tuple[str, int, int]]:
    """单段超预算的逐级降级切分（V2.0 核心）。

    降级链：
        1. 句边界软切（_soft_windows：整句归整打包）；
        2. 单句超预算 → 子句边界软切（_clause_or_token_windows 前半）；
        3. 单子句超预算 → token 窗口硬切（_token_windows）。

    ``_seen_sentence`` 防递归死循环：已从句级降下来的片段不再尝试句级。
    """
    if not _seen_sentence:
        return _soft_windows(u, max_tokens, overlap_tokens)
    return _clause_or_token_windows(u, max_tokens, overlap_tokens)


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
       要注入生成上下文），"切片 token 上限"必须是用户看到的那个数；
    2. 重叠按单元尾部回填（保持段落/条款完整，不切断）；
    3. 输出携带字符偏移（供父子挂载）；
    4. 单元超预算走四级降级（句→子句→token 硬切），段内尽量不切断。

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
            # 单元（自然段）本身超预算：先落库已有缓冲，再对该单元做
            # 【四级降级切分】——优先整句归整，单句超预算才子句，再不行
            # 才 token 窗口硬切（保句子/子句完整性，段落优先的代价控制）
            _flush()
            for piece, p_start, p_end in _split_unit_graceful(u, unit_budget, overlap_tokens):
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
    small_max_tokens: int = 300,
    small_overlap_tokens: int = 40,
    large_max_tokens: int = 2500,
    large_overlap_tokens: int = 200,
    *,
    rebuild_paragraphs_flag: bool = True,
) -> tuple[list[DualChunk], list[DualChunk]]:
    """双粒度切分：返回 (小块列表, 大块列表)，均带正文偏移。

    Args:
        text: 文档正文（已去除版式噪音）。
        prefix: 文档级上下文前缀（标题｜发文机关｜日期）。
        strategy: 切分策略（默认 paragraph=自然段优先，段内不切断）。
        small_max_tokens: 小块 token 上限（配置中心 retrieval.small_max_tokens，
            用户需求区间 100–400；**含前缀的完整切片预算**）。
        small_overlap_tokens: 小块尾重叠 token 预算。
        large_max_tokens: 大块 token 上限（配置中心 retrieval.large_max_tokens，
            用户需求区间 400–4000；同样含前缀）。
        large_overlap_tokens: 大块尾重叠 token 预算。
        rebuild_paragraphs_flag: 是否把物理行重组为自然段（配置中心
            ``chunk_paragraph_rebuild``，默认开）。

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

    units = _units_with_offsets(text, strategy, rebuild=rebuild_paragraphs_flag)
    if not units:
        return [], []

    # 批量 token 计数一次，两级粒度复用（Rust 批量接口，避免逐单元调用开销）
    counts = _count_tokens([u.text for u in units])
    for u, c in zip(units, counts, strict=True):
        u.tokens = max(c, 1)

    # 前缀开销（前缀 + 换行分隔）：每切片的固定 token 成本，计入预算
    overhead = max(_count_tokens([f"{prefix}\n"])[0], 1)

    small = _merge_budgeted(units, prefix, small_max_tokens, overhead, small_overlap_tokens, "small")
    large = _merge_budgeted(units, prefix, large_max_tokens, overhead, large_overlap_tokens, "large")
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
