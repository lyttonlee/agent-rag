"""切片：带"上下文前缀"的中文公文切片。

设计要点：
- 上下文前缀 = 标题｜发文机关｜日期｜标题路径，
  拼接在每个切片前，是提升中文公文检索质量性价比最高的一招。
- 按段落 / 字数切分，避免切断句子（尽量在段落边界切）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    content: str
    prefix: str


# 段落分隔（中文换行 / 空行）
_SPLIT_RE = re.compile(r"\n\s*\n|\u3000{2,}|(?<=[。！？；])\s*")


def build_prefix(title: str, issuer: str | None, publish_date: str | None) -> str:
    parts = [p for p in [title, issuer, publish_date] if p]
    return "｜".join(parts)


def chunk_text(
    text: str,
    prefix: str,
    max_chars: int = 800,
    overlap: int = 80,
) -> list[Chunk]:
    """将正文切分为带前缀的片段。

    策略：先按句子/段落边界粗分，再按 max_chars 合并，边界重叠 overlap 字符。
    """
    paras = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    chunks: list[Chunk] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) <= max_chars:
            buf = f"{buf}\n{para}" if buf else para
        else:
            if buf:
                chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
            # 重叠：保留尾部若干字符作为上下文衔接
            buf = (buf[-overlap:] + "\n" + para) if overlap else para
    if buf:
        chunks.append(Chunk(content=f"{prefix}\n{buf}", prefix=prefix))
    return chunks
