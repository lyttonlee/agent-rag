"""计划器（Planner）：把复杂任务拆解为有序步骤。

拆解方式与意图路由同构——规则模板优先，LLM 兜底，失败退化为单步直答：
- 比较类：尽量从 query 中拆分对比双方（"X 和 Y 的差异" → 分别检索 X / Y）；
  拆分不出双方时退化为"整体检索 + 对比分析"两步；
- 汇总类：全面检索 → 汇总清单；
- 排查类：检索现象原因 → 检索处置流程 → 汇总排查路径；
- 其他/LLM 兜底：让模型输出 JSON 步骤列表（≤max_steps 步，3s 超时）；
- 任何失败都退化为 ``[retrieve(query)]`` 单步——最终答案由汇总器生成，
  等价于直答，绝不让"拆解失败"变成"任务失败"。

步骤数硬上限由 AgentConfig.max_steps 控制（默认 6，防失控）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent.config import AgentConfig
from app.services.model_provider import ModelProvider

logger = logging.getLogger(__name__)

# 步骤动作类型（P2 仅实现 retrieve / generate；verify 为后续扩展预留）
ACTION_RETRIEVE = "retrieve"
ACTION_GENERATE = "generate"
_ACTIONS = {ACTION_RETRIEVE, ACTION_GENERATE}


@dataclass
class Step:
    """一个执行步骤。

    Attributes:
        id: 步骤标识（s1/s2/...，前端步骤条 key）。
        action: 步骤类型（retrieve=检索素材 / generate=子任务生成）。
        query: 步骤任务描述（检索步=子问题；生成步=子任务）。
        status: pending / running / done / failed（执行过程中动态更新）。
    """

    id: str
    action: str
    query: str
    status: str = "pending"


@dataclass
class PlanResult:
    """拆解结果：steps + 拆解通道（审计/前端展示用）。"""

    steps: list[Step] = field(default_factory=list)
    channel: str = "rule"


async def plan_steps(db: AsyncSession, query: str, cfg: AgentConfig) -> PlanResult:
    """把复杂任务拆解为步骤列表（规则模板 → LLM 兜底 → 单步直答降级）。"""
    text = (query or "").strip()
    if not text:
        return PlanResult([Step("s1", ACTION_RETRIEVE, query or "")], "default")

    # ---- 规则模板 ----
    if re.search(r"对比|比较|差异|区别|不同点|哪个更|优劣", text):
        sides = _split_comparison_sides(text)
        if len(sides) >= 2:
            return PlanResult(
                [
                    Step("s1", ACTION_RETRIEVE, f"{sides[0]}（{text}）"),
                    Step("s2", ACTION_RETRIEVE, f"{sides[1]}（{text}）"),
                    Step("s3", ACTION_GENERATE, f"基于前两步材料，对「{sides[0]}」与「{sides[1]}」做对比分析"),
                ],
                "rule",
            )
        return PlanResult(
            [
                Step("s1", ACTION_RETRIEVE, text),
                Step("s2", ACTION_GENERATE, f"围绕「{text}」做对比分析，列出异同点"),
            ],
            "rule",
        )

    if re.search(r"汇总|统计|清单|一览|全部列出|都有哪些", text):
        return PlanResult(
            [
                Step("s1", ACTION_RETRIEVE, f"{text}（尽量全面）"),
                Step("s2", ACTION_GENERATE, f"汇总「{text}」的相关内容，形成清单"),
            ],
            "rule",
        )

    if re.search(r"为什么|为何|如何排查|怎么排查|排查|原因|定位|失败原因", text):
        return PlanResult(
            [
                Step("s1", ACTION_RETRIEVE, f"{text}（现象与原因）"),
                Step("s2", ACTION_RETRIEVE, f"{text}（处置流程与规范）"),
                Step("s3", ACTION_GENERATE, f"汇总「{text}」的排查路径与处置建议"),
            ],
            "rule",
        )

    # ---- LLM 兜底拆解 ----
    try:
        steps = await asyncio.wait_for(_llm_plan(text, cfg.max_steps), timeout=3.0)
        if steps:
            return PlanResult(steps, "llm")
    except asyncio.TimeoutError:
        logger.info("LLM 计划超时，退化为单步直答")
    except Exception as exc:
        logger.warning("LLM 计划失败，退化为单步直答：%s", exc)

    # ---- 降级：单步检索（汇总器负责最终回答，等价直答）----
    return PlanResult([Step("s1", ACTION_RETRIEVE, text)], "fallback")


def _split_comparison_sides(text: str) -> list[str]:
    """从比较类 query 中拆分对比双方（"X 和 Y 的差异" → [X, Y]）。

    拆分是尽力而为：失败时返回空列表，由调用方走通用比较模板。
    """
    m = re.search(
        r"(?:对比|比较)?\s*(.{2,40}?)\s*(?:和|与|跟|vs|VS|,|，)\s*(.{2,40}?)"
        r"(?:的)?(?:差异|区别|不同点|不同|优劣|对比|比较)?\s*$",
        text,
    )
    if not m:
        return []
    left, right = m.group(1).strip("的是了吗呢吧，。？ "), m.group(2).strip("的是了吗呢吧，。？ ")
    # 过滤明显不成立的拆分（某一侧为空或含句读）
    if not left or not right:
        return []
    if any(ch in left + right for ch in "？?！!"):
        return []
    return [left, right]


_LLM_PLAN_SYSTEM = (
    "你是任务拆解器。把用户的复杂任务拆解为有序执行步骤，只输出 JSON 数组，不要任何其他文字。\n"
    '格式：[{"action":"retrieve|generate","query":"步骤描述"}]\n'
    "规则：\n"
    "1. retrieve 步骤用于检索知识库素材，query 写具体可检索的子问题；\n"
    "2. generate 步骤用于基于已检索材料完成子任务（分析/对比/汇总），query 写子任务目标；\n"
    "3. 步骤数不超过 %d，顺序执行，最后一步通常是 generate 汇总；\n"
    "4. 只需要一步就能完成的任务，返回单个 retrieve 步骤。"
)


async def _llm_plan(query: str, max_steps: int) -> list[Step]:
    """LLM 兜底拆解：输出 JSON 步骤列表；不可解析/超限由调用方降级。"""
    gen = ModelProvider(role="gen")
    raw = await gen.generate(
        [
            {"role": "system", "content": _LLM_PLAN_SYSTEM % max_steps},
            {"role": "user", "content": query[:800]},
        ],
        temperature=0,
        max_tokens=512,
    )
    steps = _parse_steps_json(raw, max_steps)
    return steps


def _parse_steps_json(raw: str, max_steps: int) -> list[Step]:
    """解析 LLM 步骤列表（宽容取第一个 [...] 片段；过滤非法步骤）。"""
    if not raw:
        return []
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    steps: list[Step] = []
    for i, item in enumerate(data[:max_steps], start=1):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", ACTION_RETRIEVE)).strip().lower()
        if action not in _ACTIONS:
            action = ACTION_RETRIEVE
        q = str(item.get("query", "")).strip()
        if not q:
            continue
        steps.append(Step(f"s{i}", action, q[:200]))
    return steps
