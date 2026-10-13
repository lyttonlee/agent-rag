"""Agent 运行时配置：从配置中心（ConfigKV category=agent）读取，缺省用代码默认值。

设计要点：
- 运维在「配置中心 → Agent 设置」调整开关与参数，保存即生效，无需发版；
- 配置缺失/停用/解析异常一律回退代码默认值——Agent 是增强层，
  任何配置问题都不允许把主链路带崩（降级矩阵的第一行）。
- 默认值（用户 2026-10-13 确认）：agent 总开关默认开启；LLM 分类默认开启，
  3s 超时；复杂任务步骤上限 6 步。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConfigKV

logger = logging.getLogger(__name__)

# 配置中心中的分类名（与前端「配置中心 → Agent 设置」页签对应）
AGENT_CONFIG_CATEGORY = "agent"


@dataclass(frozen=True)
class AgentConfig:
    """Agent 运行时配置（全部有安全默认值）。"""

    enabled: bool = True                    # Agent 总开关：关闭后完全走旧直答链路
    llm_classify: bool = True               # 规则通道不自信时是否允许 LLM 兜底分类
    llm_classify_timeout_ms: int = 3000     # LLM 分类超时（超时降级 direct_qa）
    max_steps: int = 6                      # 复杂任务步骤硬上限（防失控）


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


async def load_agent_config(db: AsyncSession) -> AgentConfig:
    """读取 Agent 配置（category=agent 且 is_active）；缺项用代码默认值。

    ConfigKV.value 约定为 ``{"value": <实际值>}``，与配置中心结构化存储一致。
    """
    cfg = AgentConfig()
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
        logger.warning("Agent 配置读取失败，使用默认值：%s", exc)
        return cfg

    values: dict[str, object] = {}
    for row in rows:
        v = row.value if isinstance(row.value, dict) else {}
        values[row.key] = v.get("value")

    return AgentConfig(
        enabled=_coerce_bool(values.get("enabled"), cfg.enabled),
        llm_classify=_coerce_bool(values.get("llm_classify"), cfg.llm_classify),
        llm_classify_timeout_ms=_coerce_int(
            values.get("llm_classify_timeout_ms"), cfg.llm_classify_timeout_ms, 500, 30000
        ),
        max_steps=_coerce_int(values.get("max_steps"), cfg.max_steps, 1, 12),
    )
