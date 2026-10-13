"""意图路由器：规则通道优先 + LLM 通道兜底（双通道，规则不自信才问模型）。

设计铁律（与《Agent 意图路由与分步执行方案 V1.1》一致）：
- 规则通道零成本、零延迟、确定性高，覆盖 80% 以上常见输入；
- 规则未命中任何信号时（陈述句、无标点短输入等）才走 LLM 分类，
  且 3s 超时（可配置）——超时/失败/输出不可解析一律降级 direct_qa，
  保证"Agent 判断不了也能问答"（G4 不破底线）；
- 显式信号优先级最高：请求带 template_doc_type 时直接判定 draft
  （用户显式选择优先于推断）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent.config import AgentConfig
from app.services.agent.intents import get_spec
from app.services.model_provider import ModelProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentResult:
    """意图分类结果。

    Attributes:
        intent: 意图标识（未注册意图由调用方降级处理）。
        confidence: 置信度 0-1（规则通道按信号强度给保守值）。
        channel: 分类通道（explicit / rule / llm / default / disabled）。
        reason: 人类可读的判定依据（审计 detail 用）。
    """

    intent: str
    confidence: float
    channel: str
    reason: str = ""


# ---- 规则通道信号表（按优先级从上到下匹配，先命中先返回）----

# 起草动词：出现即视为强起草意图（公文文稿类创作）
_DRAFT_VERB = re.compile(r"起草|拟稿|撰写|编写|编制|写一份|写一篇|写个|写一版|生成一份|生成一篇|出一份|行文|发文")

# 任务动词（非公文文稿的"完成任务"类：设计/整理/制定/完成…）
_TASK_VERB = re.compile(r"创作|设计一|设计方案|整理成|整理一份|制定|拟定|做一份|做一个|搞一个|完成一|帮我完成|执行方案|策划")

# 祈使/请求标记：出现即使用户是在"要求办事"而非"提问"
_IMPERATIVE = re.compile(r"帮我|请|麻烦|给我|帮忙|来一|出一份|做一|搞一个|整个|弄一|劳烦")

# 疑问标记：出现且无祈使标记时，视为"询问做法"（如"通知怎么写"）而非任务请求
_QUESTION_MARK = re.compile(r"怎么|如何|什么是|是什么|为什么|为何|哪些|多少|吗|呢|？|\?|啥时候|哪里|谁")

# 复杂任务信号（任一命中即 complex_task）
_COMPLEX_SIGNALS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"对比|比较|差异|区别|不同点|哪个更|优劣|vs|VS"), "比较"),
    (re.compile(r"汇总|统计|清单|一览|全部列出|都有哪些|分别.*(说明|列出|汇总)"), "汇总"),
    (re.compile(r"为什么|为何|如何排查|怎么排查|排查|原因|定位|失败原因"), "排查"),
]
# 多问句：≥2 个问号视为复合问题
_MULTI_QUESTION = re.compile(r"[？?]")

# 闲聊信号：短输入 + 寒暄模式（避免误伤长 query）
_CHITCHAT = re.compile(
    r"^(你好|您好|hi|hello|嗨|喂|谢谢|感谢|多谢|辛苦|麻烦了|再见|拜拜|bye)"
    r"|你是谁|你叫什么|自我介绍|你能做什么|你会什么"
)

# LLM 分类提示词：要求严格 JSON 输出，温度 0，max_tokens 64
_LLM_CLASSIFY_SYSTEM = (
    "你是企业公文知识库的意图分类器。根据用户输入判断意图，只输出 JSON，不要任何其他文字。\n"
    'JSON 格式：{"intent": "<意图>", "confidence": <0到1的小数>}\n'
    "意图取值与判定标准：\n"
    "- direct_qa：希望获得确切回答的单一问题（事实/定义/流程/规定查询；"
    "包括'通知怎么写''格式有什么要求'这类询问做法的提问）；\n"
    "- complex_task：需要多步骤完成的复杂任务（比较、汇总、排查、多条件检索、"
    "把材料整理成表格、设计一个方案等'完成任务'型诉求）；\n"
    "- draft：公文起草（用户明确要求生成文稿：帮我写/请起草/生成一份通知…）；\n"
    "- chitchat：寒暄、问候、自我介绍等与知识库无关的对话；\n"
    "- out_of_scope：超出企业公文知识库能力边界的问题（如天气、股价、娱乐）。\n"
    "判定要点：'询问怎么做'是 direct_qa；'明确要求生成/完成某物'是 draft 或 complex_task。"
)

# 意图 → LLM 输出中的合法取值（与 intents.py 注册表保持一致；
# 新增意图时在此补充即可参与 LLM 分类）
_LLM_INTENT_ALIASES = {
    "direct_qa": "direct_qa",
    "complex_task": "complex_task",
    "draft": "draft",
    "chitchat": "chitchat",
    "out_of_scope": "out_of_scope",
}


class IntentRouter:
    """意图路由器（无状态，可按请求实例化）。"""

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg

    async def classify(
        self,
        db: AsyncSession,
        query: str,
        *,
        template_doc_type: str | None = None,
    ) -> IntentResult:
        """判定用户意图：规则优先，规则不自信时 LLM 兜底，失败降级 direct_qa。

        任务/提问区分（用户 2026-10-13 反馈：创作、完成任务型诉求不能被
        误判为 direct_qa——后者无材料时会拒答，而任务型应走自身能力
        完成 + 甄别声明）：
        - 任务动词（起草/写一份/创作/设计/整理成…）+ 祈使标记（帮我/请…）
          → 任务请求（"帮我写一份通知，要注意哪些格式？"仍是任务）；
        - 任务动词 + 无疑问标记 → 任务请求（"写一份通知"）；
        - 任务动词 + 疑问标记 + 无祈使 → 询问做法（"通知怎么写"），
          按 direct_qa 处理；
        - 无任务动词的祈使（"请介绍一下策略"）仍是提问，不当任务处理。
        """
        text = (query or "").strip()
        if not text:
            return IntentResult("direct_qa", 1.0, "default", "空输入按直答处理")

        # 1) 显式信号：文种模板 → draft（用户显式选择优先于推断）
        if template_doc_type:
            return IntentResult("draft", 1.0, "explicit", f"显式文种 {template_doc_type}")

        # 2) 任务请求判定（创作/起草/完成任务 vs 询问做法）
        #    任务动词 + （祈使标记 或 无疑问标记）→ 任务请求；
        #    任务动词 + 疑问标记 + 无祈使 → 询问做法（"通知怎么写"→ direct_qa）；
        #    无任务动词的祈使（"请介绍一下策略"）仍是提问，不当任务处理。
        has_imperative = bool(_IMPERATIVE.search(text))
        has_question = bool(_QUESTION_MARK.search(text))
        has_task_verb = bool(_DRAFT_VERB.search(text) or _TASK_VERB.search(text))
        is_task_request = has_task_verb and (has_imperative or not has_question)
        if is_task_request:
            if _DRAFT_VERB.search(text):
                return IntentResult("draft", 0.9, "rule", "命中起草动词")
            return IntentResult("complex_task", 0.85, "rule", "命中任务动词")

        # 3) 复杂任务信号 → complex_task
        for pattern, kind in _COMPLEX_SIGNALS:
            if pattern.search(text):
                return IntentResult("complex_task", 0.85, "rule", f"命中{kind}信号")
        # 多问句（≥2 个问号）视为复合问题
        if len(_MULTI_QUESTION.findall(text)) >= 2:
            return IntentResult("complex_task", 0.8, "rule", "多问句")

        # 4) 闲聊信号 → chitchat
        if _CHITCHAT.search(text) and len(text) <= 30:
            return IntentResult("chitchat", 0.9, "rule", "命中寒暄模式")

        # 5) 规则不自信 → LLM 通道兜底（可配置关闭；关闭或失败均降级 direct_qa）
        if self.cfg.llm_classify:
            try:
                result = await asyncio.wait_for(
                    self._llm_classify(text),
                    timeout=max(0.5, self.cfg.llm_classify_timeout_ms / 1000),
                )
                if result is not None:
                    return result
            except asyncio.TimeoutError:
                logger.info("LLM 意图分类超时（%dms），降级 direct_qa", self.cfg.llm_classify_timeout_ms)
            except Exception as exc:
                logger.warning("LLM 意图分类失败，降级 direct_qa：%s", exc)
        return IntentResult("direct_qa", 0.5, "default", "规则未命中且 LLM 不可用，按直答处理")

    async def _llm_classify(self, text: str) -> IntentResult | None:
        """LLM 通道：一次轻量分类调用（temperature=0，max_tokens=64）。

        返回 None 表示"分类不可用"（调用方降级）；返回 IntentResult 表示
        分类成功（含 output 不可解析时的兜底 direct_qa 结果）。
        """
        gen = ModelProvider(role="gen")
        raw = await gen.generate(
            [
                {"role": "system", "content": _LLM_CLASSIFY_SYSTEM},
                {"role": "user", "content": text[:500]},
            ],
            temperature=0,
            max_tokens=64,
        )
        parsed = _parse_intent_json(raw)
        if parsed is None:
            # 输出不可解析：保可用，按直答处理（G4）
            return IntentResult("direct_qa", 0.4, "llm", "LLM 输出不可解析，按直答处理")
        intent, confidence = parsed
        if get_spec(intent) is None:
            return IntentResult("direct_qa", 0.4, "llm", f"LLM 输出未注册意图 {intent}，按直答处理")
        return IntentResult(intent, confidence, "llm", f"LLM 分类 {intent}")


def _parse_intent_json(raw: str) -> tuple[str, float] | None:
    """从模型输出中解析 {"intent": ..., "confidence": ...}。

    宽容策略：允许 JSON 前后夹杂文字（取第一个 {...} 片段）；
    完全无法解析时返回 None。
    """
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent", "")).strip().lower()
    intent = _LLM_INTENT_ALIASES.get(intent, intent)
    try:
        confidence = float(data.get("confidence", 0.6))
    except (TypeError, ValueError):
        confidence = 0.6
    return intent, max(0.0, min(1.0, confidence))
