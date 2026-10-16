"""深度推理问答（ReAct）运行时配置：从配置中心（ConfigKV category=agent）读取。

设计要点（与 agent/config.py 同款模式）：
- 运维在「配置中心 → Agent 设置」调整，**保存即生效、无需发版**；
- 配置缺失/停用/解析异常一律回退代码默认值——深度推理是增强层，
  任何配置问题都不允许把问答主链路带崩（降级到普通问答）；
- 用户 2026-10-16 确认的默认值：**默认全开**（enabled=True）+ 双档位
  （fast=2 轮/60s，deep=5 轮/180s），前端 DeepQa 页可切档、可整体关
  深度推理（切回普通问答）。

ConfigKV.value 约定为 ``{"value": <实际值>}``，与配置中心结构化存储一致。
key 均带 ``deep_`` 前缀，与 AgentConfig 的 key（enabled/max_steps 等）
共存于同一 category，互不覆盖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConfigKV

logger = logging.getLogger(__name__)

# 复用 Agent 配置分类（与前端「配置中心 → Agent 设置」页签对应）
AGENT_CONFIG_CATEGORY = "agent"

# 合法档位白名单（防手滑配置把档位解析带崩）
REACT_MODES = ("fast", "deep")


@dataclass(frozen=True)
class ReactConfig:
    """深度推理运行时配置（全部有安全默认值）。

    轮数与预算按档位分离（fast/deep），其余参数两档共享。
    """

    enabled: bool = True                    # 总开关（默认全开，用户 2026-10-16 确认）
    default_mode: str = "fast"              # 默认档位：fast=快速模式
    # 快速档（fast）：轻量对比/双跳问题，低延迟优先
    fast_max_rounds: int = 2
    fast_budget_ms: int = 60000
    # 深度档（deep）：多跳/排查/多实体综合，质量优先
    deep_max_rounds: int = 5
    deep_budget_ms: int = 180000
    decision_timeout_ms: int = 20000        # 单轮决策调用（JSON）超时
    analyze_timeout_ms: int = 10000         # 查询分析调用超时
    retrieve_top_n: int = 6                 # 每轮知识库检索召回条数
    obs_hits: int = 5                       # 每轮观测注入 prompt 的命中条数上限
    obs_snippet_chars: int = 160            # 观测 snippet 截断（防上下文膨胀）
    max_citations: int = 12                 # 最终引用条数上限
    temperature: float = 0.2                # 决策调用温度（低温保格式稳定）

    def for_mode(self, mode: str | None) -> tuple[int, int]:
        """解析档位 → (max_rounds, total_budget_ms)。

        未知/缺省档位回落默认档（default_mode），再不行回落 fast 参数。
        """
        m = (mode or self.default_mode or "fast").strip().lower()
        if m not in REACT_MODES:
            m = self.default_mode if self.default_mode in REACT_MODES else "fast"
        if m == "deep":
            return self.deep_max_rounds, self.deep_budget_ms
        return self.fast_max_rounds, self.fast_budget_ms


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


def _coerce_mode(value, default: str) -> str:
    """档位白名单校验：非法值回退默认档。"""
    if isinstance(value, str) and value.strip().lower() in REACT_MODES:
        return value.strip().lower()
    return default


# 启动种子默认值（与 ReactConfig 字段一一对应）
REACT_SEED: dict[str, object] = {
    "deep_reason_enabled": True,
    "deep_default_mode": "fast",
    "deep_fast_max_rounds": 2,
    "deep_fast_budget_ms": 60000,
    "deep_deep_max_rounds": 5,
    "deep_deep_budget_ms": 180000,
    "deep_decision_timeout_ms": 20000,
    "deep_analyze_timeout_ms": 10000,
    "deep_retrieve_top_n": 6,
    "deep_obs_hits": 5,
    "deep_obs_snippet_chars": 160,
    "deep_max_citations": 12,
    "deep_temperature": 0.2,
}


async def load_react_config(db: AsyncSession) -> ReactConfig:
    """读取深度推理配置（category=agent 且 is_active）；缺项用代码默认值。"""
    cfg = ReactConfig()
    try:
        rows = (
            await db.execute(
                select(ConfigKV).where(
                    ConfigKV.category == AGENT_CONFIG_CATEGORY,
                    ConfigKV.is_active.is_(True),
                )
            )
        ).scalars().all()
    except Exception as exc:  # 配置读取失败不影响主链路
        logger.warning("深度推理配置读取失败，使用默认值：%s", exc)
        return cfg

    values: dict[str, object] = {}
    for row in rows:
        v = row.value if isinstance(row.value, dict) else {}
        values[row.key] = v.get("value")

    return ReactConfig(
        enabled=_coerce_bool(values.get("deep_reason_enabled"), cfg.enabled),
        default_mode=_coerce_mode(values.get("deep_default_mode"), cfg.default_mode),
        fast_max_rounds=_coerce_int(values.get("deep_fast_max_rounds"), cfg.fast_max_rounds, 1, 12),
        fast_budget_ms=_coerce_int(values.get("deep_fast_budget_ms"), cfg.fast_budget_ms, 10000, 300000),
        deep_max_rounds=_coerce_int(values.get("deep_deep_max_rounds"), cfg.deep_max_rounds, 1, 12),
        deep_budget_ms=_coerce_int(values.get("deep_deep_budget_ms"), cfg.deep_budget_ms, 10000, 600000),
        decision_timeout_ms=_coerce_int(
            values.get("deep_decision_timeout_ms"), cfg.decision_timeout_ms, 3000, 120000
        ),
        analyze_timeout_ms=_coerce_int(
            values.get("deep_analyze_timeout_ms"), cfg.analyze_timeout_ms, 3000, 60000
        ),
        retrieve_top_n=_coerce_int(values.get("deep_retrieve_top_n"), cfg.retrieve_top_n, 1, 20),
        obs_hits=_coerce_int(values.get("deep_obs_hits"), cfg.obs_hits, 1, 10),
        obs_snippet_chars=_coerce_int(
            values.get("deep_obs_snippet_chars"), cfg.obs_snippet_chars, 60, 500
        ),
        max_citations=_coerce_int(values.get("deep_max_citations"), cfg.max_citations, 1, 30),
        temperature=_coerce_float(values.get("deep_temperature"), cfg.temperature, 0.0, 1.0),
    )


async def ensure_react_configs(db: AsyncSession) -> int:
    """启动种子：写入深度推理默认配置（幂等，不覆盖用户已改值）。

    与 runtime_config.ensure_default_configs 同款语义：只补缺项，
    不改已存在项——用户在配置中心调过的值永不被种子冲掉。
    """
    added = 0
    for key, val in REACT_SEED.items():
        existing = (
            await db.execute(
                select(ConfigKV).where(
                    ConfigKV.category == AGENT_CONFIG_CATEGORY,
                    ConfigKV.key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ConfigKV(
                    category=AGENT_CONFIG_CATEGORY,
                    key=key,
                    value={"value": val},
                    is_active=True,
                )
            )
            added += 1
    if added:
        await db.commit()
    return added
