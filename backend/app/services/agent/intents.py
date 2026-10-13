"""Agent 意图分类体系（可扩展注册表）。

设计决策（用户 2026-10-13 确认）：
- 意图枚举认可 direct_qa / complex_task / draft / chitchat / out_of_scope 五类，
  但**必须为后期扩展留足空间**：新增意图只需两步——
    1. ``register_intent(IntentSpec(...))`` 注册一条意图规格；
    2. 在执行面（如 services/chat.py 的 ``_CHAT_STRATEGIES``）登记一个策略函数。
  路由规则、编排骨架、SSE 协议均无需改动（未知意图自动降级为直答策略）。
- 每个意图声明其执行属性（是否需要检索、无材料时是否允许模型自身能力兜底），
  供编排层做统一决策，避免把意图语义散落在 if-else 里。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntentSpec:
    """意图规格：一条意图的完整语义声明。

    Attributes:
        name: 意图标识（如 ``direct_qa``），全系统唯一。
        label: 中文标签，用于前端意图徽标与审计可读性。
        strategy: 执行策略标识（``direct_answer`` / ``step_plan`` / ``draft`` /
            ``chitchat`` / ``refuse``），执行面按此分发；未知策略按直答处理。
        needs_retrieval: 是否走知识库检索（chitchat / out_of_scope 不走，省token）。
        allows_self_capability: 检索无有用信息时，是否允许改用模型自身能力
            完成任务（任务型意图允许，确切回答型不允许——不能编造）。
        description: 意图说明（配置中心/文档展示用）。
    """

    name: str
    label: str
    strategy: str
    needs_retrieval: bool = True
    allows_self_capability: bool = False
    description: str = ""


# 意图注册表：name -> IntentSpec。模块加载时完成内置五类注册。
_INTENT_SPECS: dict[str, IntentSpec] = {}


def register_intent(spec: IntentSpec) -> IntentSpec:
    """注册一条意图（后注册的同名意图覆盖旧定义，便于热更新扩展）。

    返回 spec 本身，方便 ``INTENT_X = register_intent(...)`` 式定义。
    """
    if not spec.name or not spec.label:
        raise ValueError("IntentSpec 必须提供 name 与 label")
    _INTENT_SPECS[spec.name] = spec
    return spec


def get_spec(name: str | None) -> IntentSpec | None:
    """按标识取意图规格；未注册返回 None（调用方决定降级策略）。"""
    if not name:
        return None
    return _INTENT_SPECS.get(name)


def all_specs() -> list[IntentSpec]:
    """全部已注册意图（配置中心/文档列举用）。"""
    return list(_INTENT_SPECS.values())


def intent_label(name: str | None) -> str:
    """意图中文标签；未注册意图原样返回标识（前端可读）。"""
    spec = get_spec(name)
    return spec.label if spec else (name or "")


# ---- 内置五类意图（用户 2026-10-13 确认认可，保留扩展位）----
INTENT_DIRECT_QA = register_intent(
    IntentSpec(
        name="direct_qa",
        label="确切回答",
        strategy="direct_answer",
        needs_retrieval=True,
        allows_self_capability=False,  # 确切回答型：无材料必须委婉拒答，禁止编造
        description="单一事实/定义/流程查询：单轮检索 + 直答",
    )
)
INTENT_COMPLEX_TASK = register_intent(
    IntentSpec(
        name="complex_task",
        label="复杂任务",
        strategy="step_plan",
        needs_retrieval=True,
        allows_self_capability=True,  # 任务型：无材料时用模型自身能力完成并注明甄别
        description="多实体/多条件/比较/汇总/排查：拆解为有序步骤分步执行",
    )
)
INTENT_DRAFT = register_intent(
    IntentSpec(
        name="draft",
        label="公文起草",
        strategy="draft",
        needs_retrieval=True,
        allows_self_capability=True,  # 任务型：无材料时基于通用写作能力起草并注明甄别
        description="按文种模板生成公文文稿",
    )
)
INTENT_CHITCHAT = register_intent(
    IntentSpec(
        name="chitchat",
        label="闲聊",
        strategy="chitchat",
        needs_retrieval=False,
        allows_self_capability=False,
        description="问候/自我介绍/感谢等与知识库无关的寒暄",
    )
)
INTENT_OUT_OF_SCOPE = register_intent(
    IntentSpec(
        name="out_of_scope",
        label="超出范围",
        strategy="refuse",
        needs_retrieval=False,
        allows_self_capability=False,
        description="超出企业公文知识库能力边界的问题：明确拒答，不编造",
    )
)
