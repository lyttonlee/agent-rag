"""查询改写（P2）：弱信号触发 → 改写变体 → 重检合并。

背景（《检索优化方案 V1.0》）：检索失败的主因是"词汇鸿沟"——用户口语化
提问与公文书面语料用词不一致（如问"出事谁负责"，语料写"安全生产责任
追究"）。本模块在基础检索给出弱信号时（结果空 / 双信号异常 / rerank
top1 低于阈值，见 retrieval._is_weak）生成改写变体重检，是提升召回率
性价比最高的一环。

三种策略（与方案文档 S1/S2/S3 对应）：
- S1 synonym  同义扩展：把口语查询改写为公文语料可能的正式表述；
- S2 hyde     HyDE：生成一段"假想命中段落"，用段落的向量去检索
              （对语义鸿沟比 query 向量更鲁棒）；
- S3 decompose 子问题拆解：复杂多部查询拆成独立子问题分别检索。

auto 策略（默认）：非多部查询 → [S1, S2]（先同义再假想，覆盖两种
鸿沟形态）；多部查询 → [S3 子问题..., S1]（先拆解再同义兜底）。
轮次上限由配置中心 rewrite_max_rounds 控制（默认 2）。

降级红线（调用方 retrieval._rewrite_and_retry 保障）：改写生成失败/
超时/返回空 → 保留原检索结果，绝不阻断主链路；改写后的候选必须经
**原 query** 重排才进入最终结果（防止改写跑题）。
"""

from __future__ import annotations

import logging
import re

from app.services.model_provider import ModelProvider

logger = logging.getLogger(__name__)

# ---- 多部查询识别（决定 auto 是否优先走 S3 拆解）----
# 公文场景典型多部形态：分号/多个问号、并列连词、"两者/分别"等
_MULTI_PART_RE = re.compile(r"[；;]|？|\?|以及|并且|同时|分别|两者|其一|其二")

# 子问题行首编号剥离（"1. " / "1、" / "① " / "- " 等）
_NUMBERING_RE = re.compile(r"^\s*(?:\d+[.、]|[\d①②③④⑤⑥⑦⑧⑨⑩]+[.、]?|[-•*])\s*")

# 改写 LLM 调用参数：低温求稳、短输出（改写不是创作）
_REWRITE_TEMPERATURE = 0.3
_REWRITE_MAX_TOKENS = 300

_PROMPT_SYNONYM = (
    "你是政务公文检索专家。请将下面的用户查询改写为 1 个更适合检索"
    "政务公文知识库的表述。要求：保留核心意图；使用公文语料中可能出现的"
    "正式用语与同义词；不要回答问题，只输出改写后的查询本身。\n"
    "用户查询：{query}\n改写结果："
)

_PROMPT_HYDE = (
    "你是政务公文写作专家。请针对下面的用户查询，写一段可能在政务公文"
    "知识库中出现的假想段落（100-200字），用于向量检索匹配。"
    "不要回答问题，不要解释，只输出假想段落正文。\n"
    "用户查询：{query}\n假想段落："
)

_PROMPT_DECOMPOSE = (
    "你是政务公文检索专家。请将下面的复杂查询拆解为 2-3 个互相独立的"
    "子问题，每个子问题单独一行，不要输出编号以外的任何内容。\n"
    "用户查询：{query}\n子问题："
)


def _looks_multi_part(query: str) -> bool:
    """粗判是否多部查询（过短查询不判，避免误拆）。"""
    if len(query.strip()) < 12:
        return False
    return bool(_MULTI_PART_RE.search(query))


# 单行改写清洗时剥离开尾/包裹的引号字符（strip 按字符集处理）
_QUOTE_CHARS = "\"'「」『』“”‘’"


def _clean_variant(text: str) -> str:
    """清洗单行改写：去引号/去空白；空串返回 ""。"""
    line = (text or "").strip().strip(_QUOTE_CHARS).strip()
    return line


def _parse_lines(text: str) -> list[str]:
    """解析多行输出：逐行清洗 + 剥离行首编号 + 去空行。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = _NUMBERING_RE.sub("", raw).strip()
        line = _clean_variant(line)
        if line:
            out.append(line)
    return out


async def _llm_rewrite(prompt: str) -> str:
    """单次改写 LLM 调用（gen 角色，非流式，短输出）。"""
    gen = ModelProvider(role="gen")
    return await gen.generate(
        [{"role": "user", "content": prompt}],
        temperature=_REWRITE_TEMPERATURE,
        max_tokens=_REWRITE_MAX_TOKENS,
    )


async def _synonym_variants(query: str) -> list[str]:
    """S1 同义扩展：取模型输出的首个非空行作为改写查询。"""
    text = await _llm_rewrite(_PROMPT_SYNONYM.format(query=query))
    variants = _parse_lines(text)[:1]
    return [v for v in variants if v != query]


async def _hyde_variant(query: str) -> list[str]:
    """S2 HyDE：假想段落整体作为检索 query（向量匹配用）。"""
    text = await _llm_rewrite(_PROMPT_HYDE.format(query=query))
    doc = (text or "").strip()
    return [doc] if doc else []


async def _decompose_variants(query: str) -> list[str]:
    """S3 子问题拆解：逐行解析子问题（上限 3 个）。"""
    text = await _llm_rewrite(_PROMPT_DECOMPOSE.format(query=query))
    subs = _parse_lines(text)[:3]
    return [s for s in subs if s != query]


async def rewrite_queries(query: str, strategy: str = "auto") -> list[str]:
    """生成改写变体列表（有序、去重；空列表=不改写）。

    Args:
        query: 原始用户查询。
        strategy: auto|synonym|hyde|decompose（配置中心白名单校验过）。

    Returns:
        改写变体列表（不含原始 query 本身——重检原 query 无意义）。
        顺序即尝试优先级（调用方按轮次上限截取）。

    异常不在此处吞掉：由调用方（retrieval._rewrite_and_retry）统一
    降级为"保留原结果"，保证主链路不中断。
    """
    query = (query or "").strip()
    if not query:
        return []

    if strategy == "synonym":
        variants = await _synonym_variants(query)
    elif strategy == "hyde":
        variants = await _hyde_variant(query)
    elif strategy == "decompose":
        variants = await _decompose_variants(query)
        if not variants:
            # 单部查询拆解无意义：退化为同义扩展兜底
            variants = await _synonym_variants(query)
    else:  # auto：多部查询优先拆解，否则同义 + 假想双路
        if _looks_multi_part(query):
            variants = await _decompose_variants(query)
            variants += await _synonym_variants(query)
        else:
            variants = await _synonym_variants(query)
            variants += await _hyde_variant(query)

    # 去重（保序）+ 去空
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        key = re.sub(r"\s+", "", v)
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    logger.info("query 改写：策略=%s 原始=%r → 变体 %d 个", strategy, query[:40], len(out))
    return out
