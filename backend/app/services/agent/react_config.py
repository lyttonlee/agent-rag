"""深度推理问答（ReAct）运行时配置：从配置中心（ConfigKV category=agent）读取。

设计要点（与 agent/config.py 同款模式）：
- 运维在「配置中心 → Agent 设置」调整，**保存即生效、无需发版**；
- 配置缺失/停用/解析异常一律回退代码默认值——深度推理是增强层，
  任何配置问题都不允许把问答主链路带崩（降级到普通问答）；
- 用户 2026-10-16 确认的默认值：**默认全开**（enabled=True）+ 双档位；
- 用户 2026-10-19 变更：**取消轮数上限**——默认 ``max_rounds=0``（不限），
  让模型自己判断"信息是否足够、何时收尾"，而不是被固定轮数硬截断。
  轮数上限保留为**可选硬闸**（运维可设正数），默认不启用。
  同日起档位不再用"轮数/时长"区分，改为**检索广度**（fast=轻量，
  deep=更宽召回），轮数两档都不限。
- 提示词（理解/决策/最终生成）支持配置中心覆盖：``prompt_*`` 为空表示用
  代码内置默认值；非空则**整段替换**，`{tool_lines}`/`{max_rounds}` 等
  占位符仍由引擎注入（见 react_prompt.build_decision_system）。

ConfigKV.value 约定为 ``{"value": <实际值>}``，与配置中心结构化存储一致。
key 均带 ``deep_`` 前缀，与 AgentConfig 的 key（enabled/max_steps 等）
共存于同一 category，互不覆盖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConfigKV

logger = logging.getLogger(__name__)

# 复用 Agent 配置分类（与前端「配置中心 → Agent 设置」页签对应）
AGENT_CONFIG_CATEGORY = "agent"

# 合法档位白名单（防手滑配置把档位解析带崩）
REACT_MODES = ("fast", "deep")

# ---- 提示词配置键（2026-10-19）----
# 空字符串 = 使用代码内置默认值；非空 = 整段替换（仍注入运行时变量）。
# 分通道：问答（qa）与公文起草（draft）各自独立配置，互不影响。
PROMPT_KEYS_QA: tuple[str, ...] = (
    "deep_prompt_understand",
    "deep_prompt_decision",
    "deep_prompt_final",
)
PROMPT_KEYS_DRAFT: tuple[str, ...] = (
    "deep_prompt_draft_understand",
    "deep_prompt_draft_decision",
)
PROMPT_KEYS: tuple[str, ...] = PROMPT_KEYS_QA + PROMPT_KEYS_DRAFT
# 单段提示词长度上限（防止把超长文本塞进配置库/上下文）
PROMPT_MAX_CHARS = 20000

# 已废弃的配置键（2026-10-19 取消轮数上限）：
# 保留在库里会误导运维"改了为什么没生效"，启动时统一置为停用（is_active=False），
# 数据行保留可追溯，但加载器只读 is_active=True 的项，因此不再生效。
LEGACY_KEYS: tuple[str, ...] = (
    "deep_fast_max_rounds",
    "deep_deep_max_rounds",
    "deep_fast_budget_ms",
    "deep_deep_budget_ms",
    "deep_retrieve_top_n",
    "deep_obs_hits",
)


@dataclass(frozen=True)
class ReactConfig:
    """深度推理运行时配置（全部有安全默认值）。

    轮数与预算的语义（2026-10-19 起）：
    - ``max_rounds = 0`` → **不限轮数**（默认）。模型持续迭代直到自己
      finish，或触发护栏（连续无进展/连续决策失败/预算耗尽）。
    - ``budget_ms = 0`` → **不限时长**（默认）。真实收敛依赖护栏而非计时。
    两个 0 都是"不设人为上限"，不是"立刻结束"——不要写成 falsy 判断的
    反面（见 for_mode/引擎里的 `> 0` 判断）。
    """

    enabled: bool = True                    # 总开关（默认全开，用户 2026-10-16 确认）
    default_mode: str = "fast"              # 默认档位：fast=快速模式
    # 公文起草是否走深度推理（2026-10-19 用户要求"智能起草也按深度推理的
    # 方式、由模型自主决策"）：默认开。false 时 /v1/generate 回退单趟
    # 检索+生成（旧行为，可作降级开关）。
    draft_enabled: bool = True

    # ---- 收尾条件：默认都不设上限 ----
    max_rounds: int = 0                     # 0 = 不限轮数（2026-10-19 用户要求）
    budget_ms: int = 0                      # 0 = 不限时长

    # ---- 档位差异：检索广度（轮数不再区分档位）----
    fast_retrieve_top_n: int = 4            # 快速档每轮召回条数（低延迟优先）
    fast_obs_hits: int = 4                  # 快速档注入决策上下文条数
    deep_retrieve_top_n: int = 8            # 深度档每轮召回条数（质量优先）
    deep_obs_hits: int = 8                  # 深度档注入决策上下文条数

    # ---- 护栏（代替"轮数上限"承担收敛职责）----
    no_progress_limit: int = 3              # 连续 N 轮无新引用 → 强制收尾
    decision_timeout_ms: int = 20000        # 单轮决策调用（JSON）超时
    analyze_timeout_ms: int = 10000         # 查询分析调用超时

    # ---- 检索/生成参数 ----
    retrieve_top_n: int = 6                 # 生效值（for_mode 按档位覆盖）
    obs_hits: int = 5                       # 生效值（for_mode 按档位覆盖）
    obs_snippet_chars: int = 160            # 观测 snippet 截断（防上下文膨胀）
    max_citations: int = 12                 # 最终引用条数上限
    temperature: float = 0.2                # 决策调用温度（低温保格式稳定）

    # ---- 提示词覆盖（空 = 用代码默认）----
    prompt_understand: str = ""
    prompt_decision: str = ""
    prompt_final: str = ""
    # 起草通道覆盖（与问答通道分开：起草的"依据收集完成"语义不同于"答完问题"）
    prompt_draft_understand: str = ""
    prompt_draft_decision: str = ""

    def for_mode(self, mode: str | None) -> "ReactConfig":
        """按档位派生**生效配置**（返回新实例，不改自身）。

        未知/缺省档位回落默认档，再不行回落 fast——与旧实现保持一致。
        档位只影响检索广度；轮数/预算两档共用同一组值（默认不限）。
        """
        m = (mode or self.default_mode or "fast").strip().lower()
        if m not in REACT_MODES:
            m = self.default_mode if self.default_mode in REACT_MODES else "fast"
        if m == "deep":
            return replace(
                self, retrieve_top_n=self.deep_retrieve_top_n, obs_hits=self.deep_obs_hits
            )
        return replace(
            self, retrieve_top_n=self.fast_retrieve_top_n, obs_hits=self.fast_obs_hits
        )

    def resolved_mode(self, mode: str | None) -> str:
        """规范化档位名（SSE/接口回显用），非法值回落默认档。"""
        m = (mode or self.default_mode or "fast").strip().lower()
        if m in REACT_MODES:
            return m
        return self.default_mode if self.default_mode in REACT_MODES else "fast"


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


def _coerce_prompt(value, default: str = "") -> str:
    """提示词覆盖值：非字符串或空白 → 视为未配置（用默认值）。"""
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return ""
    return text[:PROMPT_MAX_CHARS]


# 启动种子默认值（与 ReactConfig 字段一一对应）
REACT_SEED: dict[str, object] = {
    "deep_reason_enabled": True,
    "deep_default_mode": "fast",
    # 0 = 不限（2026-10-19）：轮数与时长都不设人为上限
    "deep_max_rounds": 0,
    "deep_budget_ms": 0,
    # 档位差异 = 检索广度
    "deep_fast_retrieve_top_n": 4,
    "deep_fast_obs_hits": 4,
    "deep_deep_retrieve_top_n": 8,
    "deep_deep_obs_hits": 8,
    # 护栏
    "deep_no_progress_limit": 3,
    "deep_decision_timeout_ms": 20000,
    "deep_analyze_timeout_ms": 10000,
    # 检索/生成
    "deep_obs_snippet_chars": 160,
    "deep_max_citations": 12,
    "deep_temperature": 0.2,
    # 提示词覆盖（空 = 用代码默认）
    "deep_prompt_understand": "",
    "deep_prompt_decision": "",
    "deep_prompt_final": "",
    # 起草通道：总开关 + 提示词覆盖（2026-10-19）
    "deep_draft_enabled": True,
    "deep_prompt_draft_understand": "",
    "deep_prompt_draft_decision": "",
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

    fast_top = _coerce_int(values.get("deep_fast_retrieve_top_n"), cfg.fast_retrieve_top_n, 1, 20)
    deep_top = _coerce_int(values.get("deep_deep_retrieve_top_n"), cfg.deep_retrieve_top_n, 1, 20)
    base_top = fast_top  # 未按档位派生时的兜底值（= 默认档 fast）

    return ReactConfig(
        enabled=_coerce_bool(values.get("deep_reason_enabled"), cfg.enabled),
        default_mode=_coerce_mode(values.get("deep_default_mode"), cfg.default_mode),
        draft_enabled=_coerce_bool(values.get("deep_draft_enabled"), cfg.draft_enabled),
        # 0 = 不限：下限给 0，上限给一个"足够大但不至于跑飞"的硬顶（运维可调）
        max_rounds=_coerce_int(values.get("deep_max_rounds"), cfg.max_rounds, 0, 200),
        budget_ms=_coerce_int(values.get("deep_budget_ms"), cfg.budget_ms, 0, 86400000),
        fast_retrieve_top_n=fast_top,
        fast_obs_hits=_coerce_int(values.get("deep_fast_obs_hits"), cfg.fast_obs_hits, 1, 20),
        deep_retrieve_top_n=deep_top,
        deep_obs_hits=_coerce_int(values.get("deep_deep_obs_hits"), cfg.deep_obs_hits, 1, 20),
        no_progress_limit=_coerce_int(
            values.get("deep_no_progress_limit"), cfg.no_progress_limit, 2, 20
        ),
        decision_timeout_ms=_coerce_int(
            values.get("deep_decision_timeout_ms"), cfg.decision_timeout_ms, 3000, 120000
        ),
        analyze_timeout_ms=_coerce_int(
            values.get("deep_analyze_timeout_ms"), cfg.analyze_timeout_ms, 3000, 60000
        ),
        retrieve_top_n=base_top,
        obs_hits=_coerce_int(values.get("deep_fast_obs_hits"), cfg.obs_hits, 1, 20),
        obs_snippet_chars=_coerce_int(
            values.get("deep_obs_snippet_chars"), cfg.obs_snippet_chars, 60, 500
        ),
        max_citations=_coerce_int(values.get("deep_max_citations"), cfg.max_citations, 1, 30),
        temperature=_coerce_float(values.get("deep_temperature"), cfg.temperature, 0.0, 1.0),
        prompt_understand=_coerce_prompt(values.get("deep_prompt_understand")),
        prompt_decision=_coerce_prompt(values.get("deep_prompt_decision")),
        prompt_final=_coerce_prompt(values.get("deep_prompt_final")),
        prompt_draft_understand=_coerce_prompt(values.get("deep_prompt_draft_understand")),
        prompt_draft_decision=_coerce_prompt(values.get("deep_prompt_draft_decision")),
    )


async def ensure_react_configs(db: AsyncSession) -> int:
    """启动种子：写入深度推理默认配置（幂等，不覆盖用户已改值）。

    与 runtime_config.ensure_default_configs 同款语义：只补缺项，
    不改已存在项——用户在配置中心调过的值永不被种子冲掉。

    同时把 2026-10-19 废弃的旧键（按档位拆分的轮数/预算）**置为停用**：
    数据保留可追溯，但不再参与加载，避免运维改了旧键却"不生效"。
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

    # 停用遗留键（幂等：已停用的不再重复写）
    for key in LEGACY_KEYS:
        legacy = (
            await db.execute(
                select(ConfigKV).where(
                    ConfigKV.category == AGENT_CONFIG_CATEGORY,
                    ConfigKV.key == key,
                )
            )
        ).scalar_one_or_none()
        if legacy is not None and legacy.is_active:
            legacy.is_active = False
            logger.info("停用已废弃的深度推理配置键：%s", key)

    if added or db.dirty or db.new:
        await db.commit()
    return added


async def save_react_prompt(db: AsyncSession, key: str, content: str) -> dict:
    """保存/清除单段提示词覆盖（供 PUT /v1/chat/react/prompts 使用）。

    Args:
        db: 数据库会话。
        key: 必须是 ``PROMPT_KEYS`` 之一（防任意键写入配置库）。
        content: 提示词全文；**空字符串表示恢复默认**（删除该覆盖项）。

    Returns:
        ``{"key":..., "content":..., "source": "custom"|"default"}``

    Raises:
        ValueError: key 不在白名单内，或内容超长。
    """
    if key not in PROMPT_KEYS:
        raise ValueError(f"未知提示词键：{key}（可用：{'、'.join(PROMPT_KEYS)}）")
    text = (content or "").strip()
    if len(text) > PROMPT_MAX_CHARS:
        raise ValueError(f"提示词过长：{len(text)} 字符，上限 {PROMPT_MAX_CHARS}")

    existing = (
        await db.execute(
            select(ConfigKV).where(
                ConfigKV.category == AGENT_CONFIG_CATEGORY,
                ConfigKV.key == key,
            )
        )
    ).scalar_one_or_none()

    if not text:
        # 恢复默认：把覆盖值清空（**保留配置项行**，便于运维在配置中心
        # 始终能看到这个键；直接删行会让键"凭空消失"，反而更难理解）。
        if existing is None:
            db.add(
                ConfigKV(
                    category=AGENT_CONFIG_CATEGORY,
                    key=key,
                    value={"value": ""},
                    is_active=True,
                )
            )
        else:
            existing.value = {"value": ""}
            existing.is_active = True
        await db.commit()
        return {"key": key, "content": "", "source": "default"}

    if existing is None:
        db.add(
            ConfigKV(
                category=AGENT_CONFIG_CATEGORY,
                key=key,
                value={"value": text},
                is_active=True,
            )
        )
    else:
        existing.value = {"value": text}
        existing.is_active = True
    await db.commit()
    return {"key": key, "content": text, "source": "custom"}
