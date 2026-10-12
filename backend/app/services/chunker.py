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
"""

from __future__ import annotations

import re
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
