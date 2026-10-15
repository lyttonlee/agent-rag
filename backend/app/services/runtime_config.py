"""运行时配置中心：检索参数 + 生成提示词模板（用户 2026-10-13 需求）。

与 ``agent/config.py`` 同款模式：配置存于配置中心（ConfigKV），
运维在「配置中心」页签调整，**保存即生效、无需发版**；读取失败 /
缺项 / 停用一律回退代码默认值——配置问题不允许把主链路带崩。

两类配置：
1. ``category=retrieval``：top_k、双路召回宽度、重排开关、向量距离阈值、
   rerank 分数阈值（改写/父块注入触发线）、**长短切分参数**（小块/大块
   token 预算、总开关）、**条件父块注入参数**（开关/数量/token 预算）、
   **查询改写参数**（开关/策略/轮次/超时）。
2. ``category=prompt``：生成提示词模板（公文起草 / 智能问答 / 无材料兜底），
   值与 ``generation_prompt.py`` 中的默认文案一致，可编辑。

ConfigKV.value 约定为 ``{"value": <实际值>}``，与配置中心结构化存储一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConfigKV

logger = logging.getLogger(__name__)

RETRIEVAL_CONFIG_CATEGORY = "retrieval"
PROMPT_CONFIG_CATEGORY = "prompt"


@dataclass(frozen=True)
class RetrievalConfig:
    """检索运行时配置（全部有安全默认值，与 settings/env 默认一致）。"""

    final_top_n: int = 8                 # 最终返回条数（原 settings.retrieve_final_top_n）
    vector_top_n: int = 50               # 向量路召回宽度（原 settings.retrieve_vector_top_n）
    keyword_top_n: int = 50              # 关键词路召回宽度（原 settings.retrieve_keyword_top_n）
    rerank_enabled: bool = True          # 重排总开关（原为请求级参数，此处为全局默认）
    # 「无有用信息」判定的向量距离阈值（原硬编码于 agent/policies.py 的 0.60，
    # 标定依据：主题相关 ≈0.28 / 无关 ≈0.64~0.70，取保守值防误杀）
    vec_distance_threshold: float = 0.60
    # rerank top1 低于该分数视为「无效检索」——查询改写与父块注入的共同触发线。
    # 标定（2026-10-13，Qwen3-Reranker-0.6B + 34 篇公文语料实测）：该模型
    # **并非 0-1 归一化**——精确命中 ≈0.34、相关但表述不同（词汇鸿沟）
    # ≈0.03、完全无关 ≈0.016。取 0.15：强命中跳过增强（省改写/注入开销），
    # 词汇鸿沟型弱命中触发改写救回。换模型/语料需重新标定（评测台）。
    rerank_min_score: float = 0.15
    # ---- 长短切分（父子块，P1；单位一律 token，chonkie 计量）----
    dual_granularity_enabled: bool = True   # 总开关：false 回退单粒度 800 字符存量路径
    # 小块 token 上限（2026-10-15 由 150 上调至 300）：原 150 过小，公文常见
    # 自然段（200–500 字）会被 token 窗口硬切成多截，检索单元语义残缺；上调后
    # 多数段落可整体落入小块，兼顾精召与段落完整性。合法区间 50–400。
    small_max_tokens: int = 300
    # 小块尾重叠 token 预算（20→40）：块间接缝多回填一个短单元，缓解边界词腰斩。
    small_overlap_tokens: int = 40
    # 大块 token 上限（1200→2500）：公文一节/一章常 2000–5000 字，1200 覆盖不全，
    # 生成注入上下文不完整；上调至 2500 更接近完整一节。合法区间 400–4000。
    large_max_tokens: int = 2500
    # 大块尾重叠 token 预算（100→200）：大块间接缝衔接增强。
    large_overlap_tokens: int = 200
    # ---- 条件父块注入（P1）----
    parent_inject_enabled: bool = True      # 上下文不足时回表注入大块
    # 单次最多注入父块数（2→3）：命中小块散落多个大块时，多注入一块提升上下文覆盖。
    parent_inject_max_count: int = 3
    parent_inject_max_tokens: int = 6000    # 注入父块总 token 预算（3000→6000：大块调至 2500 后，需容纳约 2 个大块，硬顶仍防上下文无限膨胀）
    # 注入触发线与 rerank_min_score 同源（同为 rerank top1 弱相关信号）；
    # 标定依据见 rerank_min_score 注释（实测 0.15：强命中 0.34 跳过、
    # 词汇鸿沟 0.03 触发）。调大更难注入、调小更易注入。
    parent_inject_min_rerank: float = 0.15
    # ---- 查询改写（P2）----
    # 策略默认 synonym（单次 LLM ≈11s，实测 Qwen3-27B）：多数词汇鸿沟
    # 用同义扩展即可救回；hyde 多一次 LLM（+11s），auto 双路 ≈22s，
    # 追求召回时可切 auto 并调大 rewrite_timeout_ms。
    rewrite_enabled: bool = True            # 弱信号触发 query 改写重检
    rewrite_strategy: str = "synonym"       # auto|synonym|hyde|decompose
    rewrite_max_rounds: int = 2             # 改写轮次上限
    # 改写总超时（含变体生成 + 重检轮次）。实测单次 gen 调用 ≈11s：
    # synonym 默认 25s 覆盖 1 次生成 + 2 轮重检；auto 双路生成 ≈22s，
    # 需调大至 35000+。超时降级保留原结果，不阻断检索。
    rewrite_timeout_ms: int = 25000


def _coerce_bool(value, default: bool) -> bool:
    """宽松布尔解析：接受 bool / "true"/"1" 等常见形态，异常回退默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on", "开", "开启"):
            return True
        if low in ("false", "0", "no", "off", "关", "关闭"):
            return False
    return default


def _coerce_int(value, default: int, lo: int, hi: int) -> int:
    """整数解析并夹取到合法区间，异常回退默认值。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _coerce_float(value, default: float, lo: float, hi: float) -> float:
    """浮点解析并夹取到合法区间，异常回退默认值。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# 合法的查询改写策略白名单（auto=按查询特征渐进选择 S1→S2→S3）
_REWRITE_STRATEGIES = ("auto", "synonym", "hyde", "decompose")


def _coerce_strategy(value, default: str) -> str:
    """改写策略白名单校验：非法值回退默认（防手滑配置把改写带崩）。"""
    if isinstance(value, str) and value.strip().lower() in _REWRITE_STRATEGIES:
        return value.strip().lower()
    return default


async def _load_category_values(db: AsyncSession, category: str) -> dict[str, object]:
    """读取某分类下全部生效配置项，返回 {key: value}。

    约定 ConfigKV.value = {"value": <实际值>}；读取失败返回空字典（走默认值）。
    """
    try:
        rows = (
            await db.execute(
                select(ConfigKV).where(
                    ConfigKV.category == category,
                    ConfigKV.is_active.is_(True),
                )
            )
        ).scalars().all()
    except Exception as exc:  # 配置读取失败不影响主链路
        logger.warning("配置中心读取失败（%s），使用默认值：%s", category, exc)
        return {}
    values: dict[str, object] = {}
    for row in rows:
        v = row.value if isinstance(row.value, dict) else {}
        values[row.key] = v.get("value")
    return values


async def load_retrieval_config(db: AsyncSession) -> RetrievalConfig:
    """读取检索配置（category=retrieval 且 is_active）；缺项用代码默认值。"""
    cfg = RetrievalConfig()
    values = await _load_category_values(db, RETRIEVAL_CONFIG_CATEGORY)
    if not values:
        return cfg
    return RetrievalConfig(
        final_top_n=_coerce_int(values.get("final_top_n"), cfg.final_top_n, 1, 100),
        vector_top_n=_coerce_int(values.get("vector_top_n"), cfg.vector_top_n, 1, 500),
        keyword_top_n=_coerce_int(values.get("keyword_top_n"), cfg.keyword_top_n, 1, 500),
        rerank_enabled=_coerce_bool(values.get("rerank_enabled"), cfg.rerank_enabled),
        vec_distance_threshold=_coerce_float(
            values.get("vec_distance_threshold"), cfg.vec_distance_threshold, 0.0, 2.0
        ),
        rerank_min_score=_coerce_float(
            values.get("rerank_min_score"), cfg.rerank_min_score, 0.0, 1.0
        ),
        dual_granularity_enabled=_coerce_bool(
            values.get("dual_granularity_enabled"), cfg.dual_granularity_enabled
        ),
        small_max_tokens=_coerce_int(
            values.get("small_max_tokens"), cfg.small_max_tokens, 50, 400
        ),
        small_overlap_tokens=_coerce_int(
            values.get("small_overlap_tokens"), cfg.small_overlap_tokens, 0, 100
        ),
        large_max_tokens=_coerce_int(
            values.get("large_max_tokens"), cfg.large_max_tokens, 400, 4000
        ),
        large_overlap_tokens=_coerce_int(
            values.get("large_overlap_tokens"), cfg.large_overlap_tokens, 0, 400
        ),
        parent_inject_enabled=_coerce_bool(
            values.get("parent_inject_enabled"), cfg.parent_inject_enabled
        ),
        parent_inject_max_count=_coerce_int(
            values.get("parent_inject_max_count"), cfg.parent_inject_max_count, 1, 8
        ),
        parent_inject_max_tokens=_coerce_int(
            values.get("parent_inject_max_tokens"), cfg.parent_inject_max_tokens, 500, 16000
        ),
        parent_inject_min_rerank=_coerce_float(
            values.get("parent_inject_min_rerank"), cfg.parent_inject_min_rerank, 0.0, 1.0
        ),
        rewrite_enabled=_coerce_bool(values.get("rewrite_enabled"), cfg.rewrite_enabled),
        rewrite_strategy=_coerce_strategy(values.get("rewrite_strategy"), cfg.rewrite_strategy),
        rewrite_max_rounds=_coerce_int(
            values.get("rewrite_max_rounds"), cfg.rewrite_max_rounds, 1, 4
        ),
        rewrite_timeout_ms=_coerce_int(
            values.get("rewrite_timeout_ms"), cfg.rewrite_timeout_ms, 1000, 30000
        ),
    )


async def load_prompt_templates(db: AsyncSession) -> dict[str, str]:
    """读取提示词模板（category=prompt）；缺项返回空字典（调用方用代码默认）。

    返回 {key: 模板文本}，key ∈ {draft_system, chat_system, no_materials_system}。
    """
    values = await _load_category_values(db, PROMPT_CONFIG_CATEGORY)
    templates: dict[str, str] = {}
    for key, val in values.items():
        if isinstance(val, str) and val.strip():
            templates[key] = val
    return templates


# =========================================================================
# 启动种子：把默认配置写入配置中心（不覆盖已有配置）
# =========================================================================

# 检索配置默认值（与 RetrievalConfig 字段一一对应，供种子与前端表单使用）
RETRIEVAL_SEED: dict[str, object] = {
    "final_top_n": 8,
    "vector_top_n": 50,
    "keyword_top_n": 50,
    "rerank_enabled": True,
    "vec_distance_threshold": 0.60,
    "rerank_min_score": 0.15,
    # 长短切分（P1）
    "dual_granularity_enabled": True,
    "small_max_tokens": 150,
    "small_overlap_tokens": 20,
    "large_max_tokens": 1200,
    "large_overlap_tokens": 100,
    # 条件父块注入（P1）
    "parent_inject_enabled": True,
    "parent_inject_max_count": 2,
    "parent_inject_max_tokens": 3000,
    "parent_inject_min_rerank": 0.15,
    # 查询改写（P2）
    "rewrite_enabled": True,
    "rewrite_strategy": "synonym",
    "rewrite_max_rounds": 2,
    "rewrite_timeout_ms": 25000,
}


async def ensure_default_configs(db: AsyncSession) -> int:
    """启动种子：写入默认检索配置（幂等，不覆盖用户已改值）。

    提示词模板不种子——默认文案以代码为准（generation_prompt.py），
    用户在配置中心编辑后才落库，避免双份真相。
    """
    added = 0
    for key, val in RETRIEVAL_SEED.items():
        existing = (
            await db.execute(
                select(ConfigKV).where(
                    ConfigKV.category == RETRIEVAL_CONFIG_CATEGORY,
                    ConfigKV.key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ConfigKV(
                    category=RETRIEVAL_CONFIG_CATEGORY,
                    key=key,
                    value={"value": val},
                    is_active=True,
                )
            )
            added += 1
    if added:
        await db.commit()
    return added
