"""Agent 内核包：意图路由 + 计划 + 执行 + 汇总 + 话术策略。

对外只暴露编排层（services/chat.py、services/generation.py）需要的入口，
内部模块（router/planner/executor/aggregator/policies）可独立演进。

架构与设计决策详见《Agent 意图路由与分步执行方案 V1.1》：
- Agent 是"编排层"不是"新引擎"：检索/生成/脱敏/权限下推全部复用现有能力；
- 意图枚举可扩展：register_intent() 注册新意图，路由与编排骨架零改动；
- 降级矩阵兜底：任何环节失败都不允许把主链路带崩。
"""

from app.services.agent.aggregator import (
    build_final_messages,
    failed_step_ids,
    has_materials,
    merge_citations,
)
from app.services.agent.config import AgentConfig, load_agent_config
from app.services.agent.executor import StepArtifact, execute_plan
from app.services.agent.intents import (
    IntentSpec,
    all_specs,
    get_spec,
    intent_label,
    register_intent,
)
from app.services.agent.planner import PlanResult, Step, plan_steps
from app.services.agent.policies import (
    NO_RESULT_ANSWER,
    OUT_OF_SCOPE_ANSWER,
    TASK_DISCLAIMER,
    is_no_useful_info,
    pick_chitchat_reply,
    stream_text,
)
from app.services.agent.router import IntentResult, IntentRouter

__all__ = [
    "AgentConfig",
    "IntentResult",
    "IntentRouter",
    "IntentSpec",
    "NO_RESULT_ANSWER",
    "OUT_OF_SCOPE_ANSWER",
    "PlanResult",
    "Step",
    "StepArtifact",
    "TASK_DISCLAIMER",
    "all_specs",
    "build_final_messages",
    "execute_plan",
    "failed_step_ids",
    "get_spec",
    "has_materials",
    "intent_label",
    "is_no_useful_info",
    "load_agent_config",
    "merge_citations",
    "pick_chitchat_reply",
    "plan_steps",
    "register_intent",
    "stream_text",
]
