"""运行时配置中心：检索参数 + 生成提示词模板（用户 2026-10-13 需求）。

与 ``agent/config.py`` 同款模式：配置存于配置中心（ConfigKV），
运维在「配置中心」页签调整，**保存即生效、无需发版**；读取失败 /
缺项 / 停用一律回退代码默认值——配置问题不允许把主链路带崩。

两类配置：
1. ``category=retrieval``：top_k、双路召回宽度、重排开关、向量距离阈值、
   rerank 分数阈值（查询改写触发线，随《检索优化方案 V1.0》P1 上线）、
   改写开关与策略（P1 规划项，默认关闭）。
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
    # rerank top1 低于该分数视为「无效检索」——查询改写的触发线之一
    # （《检索优化方案 V1.0》P1；Qwen3-Reranker 归一化分数下 0.3~0.5 为弱相关带，
    #  默认 0.35 待评测台标定；改写功能未上线前该值暂不参与判定）
    rerank_min_score: float = 0.35
    # ---- 查询改写（P1 规划项，默认关闭；上线后由 services/query_rewrite.py 消费）----
    rewrite_enabled: bool = False
    rewrite_max_rounds: int = 2
    rewrite_timeout_ms: int = 8000


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
        rewrite_enabled=_coerce_bool(values.get("rewrite_enabled"), cfg.rewrite_enabled),
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
    "rerank_min_score": 0.35,
    "rewrite_enabled": False,
    "rewrite_max_rounds": 2,
    "rewrite_timeout_ms": 8000,
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
