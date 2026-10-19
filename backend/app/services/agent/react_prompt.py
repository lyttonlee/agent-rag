"""深度推理（ReAct）提示词、决策协议解析与各阶段提示词导出。

四个职责：
1. **理解阶段**提示词（``UNDERSTAND_SYSTEM``）：让模型先复述问题、列出需要
   澄清/检索的**候选方向**，并**自主判断是否必须查库**——但这只是给模型
   的参考，**不做任何强制路由**（无"单跳直接检索"降级，见下方红线）；
2. **决策阶段** system prompt（TAO 循环每轮）：严格单 JSON 输出，且明确
   告知模型"是否调用工具、调用哪个工具、是否需要打开文档看全文，
   **全部由你自主判断**"，允许第 1 轮就 finish（模型自身能力足够时）；
3. 决策输出解析（宽容 JSON：允许夹杂文字，字段校验，失败由调用方计数收敛）；
4. 观测格式化 + 最终生成 messages + TAO 全链文本 + **提示词导出**
   （供前端「提示词管理」面板展示，改 prompt 不用翻代码）。

设计红线（2026-10-16 用户明确要求，逐条对应）：
- **不降级**：不存在"分析判定单跳 → 跳过循环直接检索"的短路分支。理解
  阶段的输出仅作为**上下文提示**注入决策历史，模型仍必须自己在 TAO 循环
  里决定动作；即使理解阶段失败/超时，循环照常进行（只是少一段参考信息）。
- **工具自主权**：是否检索、用哪个工具、要不要打开文档看全文、要不要
  多轮、何时收尾——全部由模型决策。引擎只提供护栏（轮数/预算/去重），
  不代替模型做"要不要查库"的决定。
- **不拒答**：材料不足时模型必须**完成任务**（用自身知识给出可用答案并
  标注依据不足），仅在答案末尾统一附「AI 生成，请人工鉴别」声明。
- 决策协议用 **JSON 结构化输出**而非 function-calling：不依赖内网 vLLM
  是否开启 tool-call-parser，任意 OpenAI 兼容端点均可工作。
- Thought 是**显式结构化文本**（模型自主产出），前端逐轮展示——这是
  "完整推理思维链可见"的载体，与模型隐式 reasoning_content 无关。
- prompt 里所有数值（轮数）由调用方注入，配置化不散落。
- **2026-10-19**：轮数上限默认取消（``max_rounds=0`` 表示不限），
  决策提示词里的收尾规则改写为"不设轮数上限，由你判断何时信息足够"；
  三类提示词（理解/决策/最终生成）支持配置中心整段覆盖，覆盖文本中
  仍可用 `{tool_lines}` / `{max_rounds}` / `{round_rule}` / `{material_rule}`
  占位符由引擎注入（用 str.replace 而非 format，避免提示词内的 JSON
  花括号触发 KeyError）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.schemas.contracts import Citation
# 起草成稿提示词的默认文案（配置中心「提示词模板」的代码内置值）：
# react_prompt 需要它做"只读预览"的兜底，generation_prompt 不依赖本模块，
# 单向引用不成环。
from app.services.generation_prompt import (
    DEFAULT_DRAFT_SYSTEM,
    DEFAULT_NO_MATERIALS_SYSTEM,
)

# 停止动作标识（模型输出该 action 表示信息充分、进入最终生成）
TOOL_FINISH = "finish"

# ---- 理解阶段 prompt（轻量一次调用，temperature=0）----
# 注意措辞：**不给模型"路由答案"，只让它自己判断**。字段 needs_knowledge
# 是模型的**自述判断**，引擎不据此分支（不降级），仅作为提示呈现给用户看。
UNDERSTAND_SYSTEM = (
    "你是企业公文知识库的提问理解器。请先理解用户真正想要什么，只输出 JSON，不要任何其他文字。\n"
    'JSON 格式：{"restate": "用你自己的话复述用户诉求", '
    '"needs_knowledge": true 或 false, '
    '"needs_knowledge_reason": "为什么需要/不需要查知识库", '
    '"candidates": ["可选的检索/查文档方向，最多4个"], '
    '"ambiguities": ["需要向用户澄清的点，没有则空数组"]}\n'
    "判定参考：\n"
    "- 涉及本库具体文件的规定/要求/流程/数据/口径 → needs_knowledge=true；\n"
    "- 属于通用公文写作、格式常识、通用方法论，或用户已提供足够信息 → "
    "needs_knowledge=false；\n"
    "- 你只需陈述判断，**后续是否真的调用检索工具由推理阶段的智能体自行决定**，"
    "本判断不构成任何强制指令。"
)

# ---- 起草通道理解 prompt（2026-10-19 新增）----
# 与问答版的差异：复述的对象是"起草诉求"（文种/主题/对象/关键要求），
# 歧义点解释为"成稿时需留占位或说明的点"而非"要向用户澄清的点"。
UNDERSTAND_SYSTEM_DRAFT = (
    "你是企业公文知识库的起草需求理解器。请先理解用户想起草什么，只输出 JSON，不要任何其他文字。\n"
    'JSON 格式：{"restate": "用你自己的话复述起草诉求（文种/主题/对象/关键要求）", '
    '"needs_knowledge": true 或 false, '
    '"needs_knowledge_reason": "为什么需要/不需要查知识库参考", '
    '"candidates": ["可查的参考方向，最多4个"], '
    '"ambiguities": ["需求中不明确、成稿时需要留占位或说明的点，没有则空数组"]}\n'
    "判定参考：\n"
    "- 涉及本单位具体规定、口径、数据、历史文件、特定事项 → needs_knowledge=true；\n"
    "- 通用公文格式、通用写法，或用户已给足信息 → needs_knowledge=false；\n"
    "- 你只需陈述判断，**后续是否真的调用检索工具由推理阶段的智能体自行决定**，"
    "本判断不构成任何强制指令。"
)

# ---- 决策 system prompt（TAO 循环每轮）----
# 收尾规则单独抽成一段，便于"不限轮数"与"有硬上限"两种形态复用。
# 注意：{round_rule} 由 build_decision_system 注入，覆盖提示词里也可引用。
DECISION_ROUND_RULE_LIMITED = (
    "6. 信息足够就 finish，不要为问而问；本轮上限 {max_rounds} 轮。"
)
DECISION_ROUND_RULE_UNLIMITED = (
    "6. 信息足够就 finish，不要为问而问。**不设轮数上限**——需要几轮就做几轮，"
    "但禁止机械重复同一检索；若已连续多轮没有新材料，请换角度或直接基于现有"
    "材料 finish 完成任务（不得拒答）。"
)

# ---- 起草通道决策 prompt（2026-10-19 新增，公文起草与问答共用引擎但任务不同）----
# 与问答版的差异：① 智能体身份是"公文起草"而非"问答"；② finish 的语义是
# "依据已足够，交由系统成稿"；③ 明确"不得编造文号/指标/人名"的起草红线。
DECISION_SYSTEM_TMPL_DRAFT = (
    "你是企业公文知识库的「公文起草」智能体。你为一份公文收集起草依据——"
    "查知识库有没有相关规定、口径、同类文件，材料足够后由系统依据全部材料起草成文。\n"
    "每一轮你只能输出一个 JSON 对象，不得输出任何其他文字，三种形态：\n"
    '1) 调用工具：{{"thought": "本轮思考：已知什么、缺什么、下一步为什么选这个工具", '
    '"action": {{"tool": "<工具名>", "query": "给该工具的具体参数"}}}}\n'
    '2) 打开文档看全文：{{"thought": "为什么需要看这份原文", '
    '"action": {{"tool": "read_document", "query": "<chunk_id 或文档标题>"}}}}\n'
    '3) 依据已足够：{{"thought": "为什么现有材料已足够动笔（或为什么可直接按通用公文规范起草）", '
    '"action": {{"tool": "finish", "query": ""}}}}\n'
    "可用工具：\n{tool_lines}\n"
    "自主决策规则（重要）：\n"
    "1. **是否需要检索、是否需要看文档，由你根据起草需求自行判断**——"
    "通用格式/写法类请求（如只要求「写一份会议通知」且无特定口径要求）可以直接 finish；\n"
    "2. 用户给了具体单位、事项、数据时，先用 knowledge_search 查库内是否有对应"
    "规定/口径/历史文件，必要时用 read_document 核对原文——**不得凭记忆编造"
    "文号、指标、人名、发布日期**；\n"
    "3. query 必须具体，一次只查一个点；公文的结构性要素（标题/主送/正文/落款）"
    "由系统成稿时处理，你只需保证事实性依据充分；\n"
    "4. 观察结果为空或不相关时，更换关键词或换角度重试，禁止原样重复；\n"
    "5. **任何情况下都不要放弃任务**：即使查不到材料，也要 finish 让系统按通用"
    "公文规范完成起草（依据不足处由成稿阶段标注提示），最后由系统统一附加"
    "「AI 生成，请人工鉴别」声明；\n"
    "{round_rule}"
)

DECISION_SYSTEM_TMPL = (
    "你是企业公文知识库的「深度推理问答」智能体。你通过多轮调用工具收集证据，最后给出有依据的回答。\n"
    "每一轮你只能输出一个 JSON 对象，不得输出任何其他文字，三种形态：\n"
    '1) 调用工具：{{"thought": "本轮思考：已知什么、缺什么、下一步为什么选这个工具", '
    '"action": {{"tool": "<工具名>", "query": "给该工具的具体参数"}}}}\n'
    '2) 打开文档看全文：{{"thought": "为什么需要看原文而不是靠摘要", '
    '"action": {{"tool": "read_document", "query": "<chunk_id 或文档标题>"}}}}\n'
    '3) 直接作答：{{"thought": "为什么现有信息已足够（或为什么可以基于通用知识完成）", '
    '"action": {{"tool": "finish", "query": ""}}}}\n'
    "可用工具：\n{tool_lines}\n"
    "自主决策规则（重要）：\n"
    "1. **是否需要检索、是否需要看文档，由你根据问题自行判断**——"
    "不需要检索时（例如通用公文写作、格式常识、用户已给足信息）可以直接 finish；\n"
    "2. 检索命中片段不足以支撑结论时，用 read_document 打开原文核对，不要凭空推断；\n"
    "3. query 必须具体、可检索，一次只查一个点；多实体对比/多子问题分别检索；\n"
    "4. 观察结果为空或不相关时，更换关键词或换角度重试，禁止原样重复；\n"
    "5. **任何情况下都不要放弃任务**：即使始终查不到材料，也必须基于通用知识"
    "完成任务并说明依据不足，最后由系统统一附加「AI 生成，请人工鉴别」声明；\n"
    "{round_rule}"
)



@dataclass
class AnalysisResult:
    """理解阶段结果。

    **仅作参考展示与上下文提示，引擎不据此分支**（无 single_hop 降级）。
    needs_knowledge 为模型自述，字段保留是为了让前端展示"模型认为是否需要查库"。
    """

    restate: str = ""                                  # 模型复述的诉求
    needs_knowledge: bool = True                        # 模型自述：是否需要查库
    needs_knowledge_reason: str = ""                    # 判断理由
    candidates: list[str] = field(default_factory=list)  # 候选检索方向
    ambiguities: list[str] = field(default_factory=list)  # 待澄清点

    def as_hint_text(self) -> str:
        """转成注入决策历史的提示文本（供模型参考，不构成强制指令）。"""
        parts = [f"【我对提问的理解】{self.restate}"] if self.restate else []
        verdict = "需要查知识库" if self.needs_knowledge else "我认为无需查库"
        line = f"【我初步判断】{verdict}"
        if self.needs_knowledge_reason:
            line += f"（{self.needs_knowledge_reason}）"
        parts.append(line)
        if self.candidates:
            parts.append("【候选方向（仅供参考，可自行调整或否决）】\n" + "\n".join(
                f"- {c}" for c in self.candidates
            ))
        if self.ambiguities:
            parts.append("【可能的歧义（可在回答中说明，不必反问用户）】\n" + "\n".join(
                f"- {a}" for a in self.ambiguities
            ))
        parts.append("注意：以上只是我的初步分析，**下一步动作由你在本轮自行决定**。")
        return "\n".join(parts)


@dataclass
class Decision:
    """一轮决策（解析成功后的结构化结果）。"""

    thought: str
    tool: str
    query: str


def _round_rule(max_rounds: int) -> str:
    """收尾规则文本：``max_rounds <= 0`` 即不限轮数（默认形态）。"""
    if max_rounds and max_rounds > 0:
        return DECISION_ROUND_RULE_LIMITED.replace("{max_rounds}", str(max_rounds))
    return DECISION_ROUND_RULE_UNLIMITED


def _inject(template: str, variables: dict[str, str]) -> str:
    """把运行时变量注入提示词（str.replace，不用 format）。

    为什么不用 ``str.format``：提示词正文里含大量 JSON 花括号
    （``{"thought": ...}``），format 会把它们当占位符并抛 KeyError。
    replace 只认我们显式列出的 token，未识别的花括号原样保留。
    """
    text = template
    for name, value in variables.items():
        text = text.replace("{" + name + "}", value)
    return text


@dataclass(frozen=True)
class TaskPromptPack:
    """任务形态提示词包：让同一套 ReAct 引擎服务"问答"与"公文起草"两种任务。

    为什么需要它：起草与问答的 TAO 循环结构完全相同（都是自主决策调用工具
    收集依据），但智能体身份、finish 语义、停止原因的中文标签都不同——
    直接复用问答 prompt 会让起草场景的模型以为自己在"回答问题"而不是"收集
    起草依据"。因此把差异化部分收进 pack，引擎与编排零分支。

    字段：
    - channel: 通道标识（qa / draft），SSE react_start 与 trace 落库用；
    - understand_system: 理解阶段内置提示词（可被配置覆盖）；
    - decision_template: 决策阶段内置模板（可被配置覆盖）；
    - stop_labels: 停止原因 → 中文标签（缺省用 STOP_REASON_LABELS 共享表）；
    - tool_phase_labels: 工具 → 阶段播报动作名；
    - history_hint_chars: 会话历史注入决策上下文的截断长度
      （问答 500 字够用；起草需让模型知道上一稿文种与篇幅，放宽到 1200）。
    """

    channel: str
    understand_system: str
    decision_template: str
    stop_labels: dict[str, str] = field(default_factory=dict)
    tool_phase_labels: dict[str, str] = field(default_factory=dict)
    history_hint_chars: int = 500


# 问答通道（默认，2026-10-16 起）
QA_PACK = TaskPromptPack(
    channel="qa",
    understand_system=UNDERSTAND_SYSTEM,
    decision_template=DECISION_SYSTEM_TMPL,
)

# 起草通道（2026-10-19 新增：智能起草改为 ReAct 自主决策）
DRAFT_PACK = TaskPromptPack(
    channel="draft",
    understand_system=UNDERSTAND_SYSTEM_DRAFT,
    decision_template=DECISION_SYSTEM_TMPL_DRAFT,
    stop_labels={
        "final": "依据已足够（模型主动结束，开始起草）",
        "max_rounds": "达到轮数上限",
        "budget": "超出时间预算",
        "no_progress": "连续多轮无新材料，已开始起草",
        "parse_fail": "决策输出连续不可解析",
        "decision_fail": "决策调用连续失败",
    },
    tool_phase_labels={
        "knowledge_search": "检索起草依据",
        "doc_lookup": "查找候选文件",
        "read_document": "打开原文",
    },
    history_hint_chars=1200,
)


def build_decision_system(
    max_rounds: int,
    tool_lines: str,
    override: str = "",
    template: str | None = None,
) -> str:
    """组装决策 system prompt（工具清单与收尾规则由配置注入）。

    Args:
        max_rounds: 轮数硬上限；``0`` 表示不限轮数（默认）。
        tool_lines: 工具契约文本（由 react_tools.tool_lines() 生成）。
        override: 配置中心的自定义提示词；非空则整段替换默认模板，
            并注入 `{tool_lines}` / `{max_rounds}` / `{round_rule}` 三个变量。
        template: 内置模板；缺省用问答模板（``DECISION_SYSTEM_TMPL``）。
            起草通道传 ``DECISION_SYSTEM_TMPL_DRAFT``（见 TaskPromptPack）。

    Returns:
        最终注入模型的 system 文本。
    """
    variables = {
        "tool_lines": tool_lines,
        "max_rounds": str(max_rounds) if max_rounds and max_rounds > 0 else "不限",
        "round_rule": _round_rule(max_rounds),
    }
    if (override or "").strip():
        text = _inject(override.strip(), variables)
        # 自定义提示词若没引用 {tool_lines}，补一份工具清单——否则模型不知道
        # 可用工具（这是自定义提示词最容易漏的一处，宁可重复也不缺）。
        if "{tool_lines}" not in override and tool_lines not in text:
            text = f"{text}\n\n可用工具：\n{tool_lines}"
        return text
    return _inject(template if template is not None else DECISION_SYSTEM_TMPL, variables)



def _extract_json_object(raw: str) -> dict | None:
    """宽容提取第一个完整 JSON 对象（允许前后夹杂文字）。

    返回 None 表示完全找不到候选对象（调用方按解析失败计数）。
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
    return data if isinstance(data, dict) else None


def parse_decision(raw: str) -> Decision | None:
    """解析一轮决策输出；任何字段缺失/类型不符返回 None（调用方收敛）。"""
    data = _extract_json_object(raw)
    if data is None:
        return None
    thought = data.get("thought")
    action = data.get("action")
    if not isinstance(thought, str) or not isinstance(action, dict):
        return None
    tool = action.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    query = action.get("query")
    if query is None:
        query = ""
    if not isinstance(query, str):
        return None
    return Decision(
        thought=thought.strip()[:800],
        tool=tool.strip().lower()[:40],
        query=query.strip()[:300],
    )


def parse_analysis(raw: str) -> AnalysisResult | None:
    """解析理解阶段输出；不可解析返回 None（**不阻断循环**，仅少一段参考）。"""
    data = _extract_json_object(raw)
    if data is None:
        return None

    def _str_list(key: str, limit: int = 4) -> list[str]:
        v = data.get(key)
        if not isinstance(v, list):
            return []
        return [str(s).strip()[:200] for s in v if str(s).strip()][:limit]

    restate = data.get("restate")
    needs = data.get("needs_knowledge")
    reason = data.get("needs_knowledge_reason")
    return AnalysisResult(
        restate=str(restate).strip()[:300] if isinstance(restate, str) else "",
        needs_knowledge=bool(needs) if isinstance(needs, bool) else True,
        needs_knowledge_reason=str(reason).strip()[:200] if isinstance(reason, str) else "",
        candidates=_str_list("candidates"),
        ambiguities=_str_list("ambiguities"),
    )


def format_observation(*, tool: str, query: str, hits: list[Citation], quality: dict, note: str) -> str:
    """把一轮工具结果格式化为注入决策历史的 user 消息。

    hits 已在工具层截取（top obs_hits，snippet 截断由 tools 层控制长度上限）；
    quality 携带检索质量信号（向量距离/关键词命中/rerank top1），帮助模型
    判断"这一轮检得行不行"。
    """
    lines = [f'[观察] 工具 {tool}，查询 "{query}"，命中 {len(hits)} 条：']
    for i, c in enumerate(hits, 1):
        doc_no = f"（{c.doc_no}）" if c.doc_no else ""
        lines.append(f"[{i}] 《{c.title}》{doc_no} {c.snippet}")
    if quality:
        q_parts = []
        if quality.get("top_vector_distance") is not None:
            q_parts.append(f"向量距离 {quality['top_vector_distance']:.2f}")
        if quality.get("keyword_hits") is not None:
            q_parts.append(f"关键词命中 {quality['keyword_hits']}")
        if quality.get("top1_score") is not None:
            q_parts.append(f"top1 相关度 {quality['top1_score']:.2f}")
        if q_parts:
            lines.append("质量信号：" + "，".join(q_parts))
    if note:
        lines.append(f"提示：{note}")
    if not hits:
        lines.append("提醒：这不是终止条件，请换关键词/换角度重试，或基于通用知识 finish 完成作答。")
    return "\n".join(lines)


def format_process_summary(rounds_log: list[dict]) -> str:
    """检索过程摘要（最终生成 prompt 用，每轮一行）。"""
    lines: list[str] = []
    for r in rounds_log:
        thought = (r.get("thought") or "").strip()
        action = r.get("action") or {}
        tool = action.get("tool") or "-"
        query = action.get("query") or ""
        hits = r.get("observation_hits") or 0
        seg = f"- 第 {r.get('round')} 轮：{thought or '（无思考记录）'}"
        if tool == TOOL_FINISH:
            seg += " → 决定直接作答"
        else:
            seg += f" → {tool}(\"{query}\")，命中 {hits} 条"
        lines.append(seg)
    return "\n".join(lines) if lines else "（本轮未调用任何工具，智能体直接作答）"


def build_trace_text(*, analysis: AnalysisResult, rounds_log: list[dict], stop_reason: str,
                     stop_label: str, rounds: int, citations_total: int) -> str:
    """TAO 全链文本（随 assistant 消息 reasoning 落库，前端折叠面板回看）。

    格式即契约：前端 ReActTracePanel 解析「【第N轮】」前缀还原时间线，
    解析失败回落纯文本展示——两端靠此前缀约定，不要随意改格式。
    """
    head = "【提问理解】"
    if analysis.restate:
        head += analysis.restate
    head += f"｜模型自述是否需要查库：{'是' if analysis.needs_knowledge else '否'}"
    if analysis.needs_knowledge_reason:
        head += f"（{analysis.needs_knowledge_reason}）"
    if analysis.candidates:
        head += "｜候选方向：" + "；".join(analysis.candidates)

    lines = [head]
    for r in rounds_log:
        thought = (r.get("thought") or "").strip()
        action = r.get("action")
        note = r.get("observation_note") or ""
        elapsed = r.get("elapsed_ms")
        t_suffix = f"，本轮耗时 {elapsed}ms" if isinstance(elapsed, int) else ""
        if not action:
            # 决策失败/输出不可解析的轮次：无思考无行动，只留失败痕迹
            lines.append(f"【第{r.get('round')}轮】决策失败：{note or '未能形成有效决策'}")
            continue
        tool = action.get("tool") or "-"
        query = action.get("query") or ""
        hits = r.get("observation_hits") or 0
        if tool == TOOL_FINISH:
            lines.append(f"【第{r.get('round')}轮】思考：{thought or '（无思考记录）'} → 决定：直接作答")
            continue
        skipped = "（已跳过）" if action.get("skipped") else ""
        seg = f"【第{r.get('round')}轮】思考：{thought or '（无思考记录）'} → 调用：{tool}{skipped}(\"{query}\")"
        seg += f" → 观察：命中 {hits} 条{t_suffix}"
        if note:
            seg += f"（{note}）"
        lines.append(seg)

    lines.append(f"【结束】停止原因：{stop_label}（{stop_reason}）；共 {rounds} 轮；累计引用 {citations_total} 条")
    return "\n".join(lines)


def _material_rule(has_material: bool) -> str:
    """最终生成的材料使用规则（有材料/无材料两条分支）。

    无材料分支是"不拒答"红线的落点：必须完成回答 + 标注依据不足，
    而不是甩一句"无法回答"。
    """
    if has_material:
        return (
            "1. 事实性陈述必须标注引用 [n]（对应材料序号）；\n"
            "2. 材料相互矛盾时并列指出，并说明通常以更新发布的规定/上级规定为准；\n"
            "3. 材料未覆盖的内容注明「依据不足」，可以结合通用公文知识补全并说明；\n"
        )
    return (
        "1. **本次未检索到可引用的知识库材料**。你必须基于自身通用公文知识"
        "**完成对用户诉求的完整回答**，不得拒答、不得只回一句「无法回答」；\n"
        "2. 对无法确认的部分明确标注「以下为通用规范建议，知识库暂无对应规定，"
        "请以实际收到的正式文件为准」；\n"
    )


def build_final_system(has_material: bool) -> str:
    """最终生成的 system prompt（默认形态，可被配置中心整段覆盖）。"""
    return (
        "你是企业公文知识库的深度推理问答助手。以下【引用材料】"
        + ("来自多轮检索综合，" if has_material else "本次为空，")
        + "请完成对用户诉求的回答，并遵守：\n"
        + _material_rule(has_material)
        + "4. 结合对话历史理解用户指代，保持上下文连贯；\n"
        + "5. 结构清晰、结论明确、直接给出可执行结果，不要反问用户；\n"
        + "6. 系统会在你的回答末尾统一附加「AI 生成，请人工鉴别」声明，你无需自己写。"
    )


def build_final_messages(
    query: str,
    history: list[tuple[str, str]],
    citations: list[Citation],
    rounds_log: list[dict],
    system_override: str = "",
) -> list[dict]:
    """最终生成 messages：多轮材料综合 + 交叉验证 + 全局引用编号。

    citations 已由策略层按相关度去重排序并重编号 [1..N]。

    Args:
        system_override: 配置中心的自定义最终生成提示词；非空时整段替换，
            支持 `{material_rule}` 占位符（按有无材料自动注入对应规则）。
            注意：覆盖后"不拒答/AI 声明"等约束由运维自行保证。
    """
    has_material = bool(citations)
    if (system_override or "").strip():
        system = _inject(
            system_override.strip(),
            {
                "material_rule": _material_rule(has_material),
                "has_material": "有" if has_material else "无",
            },
        )
    else:
        system = build_final_system(has_material)
    messages: list[dict] = [{"role": "system", "content": system}]
    for role, content in history:
        messages.append({"role": role, "content": content[:2000]})

    context = "\n\n".join(
        f"[{i + 1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{c.snippet}"
        for i, c in enumerate(citations)
    )
    user = (
        f"【我的推理过程】\n{format_process_summary(rounds_log)}\n\n"
        f"【引用材料】\n{context or '（无）'}\n\n"
        f"【用户诉求】\n{query}"
    )
    messages.append({"role": "user", "content": user})
    return messages



# ---- 提示词导出（前端「提示词管理」面板数据源）----
# 观察消息模板（工具结果 → 决策上下文）。它是**格式化逻辑**（format_observation），
# 不是可整段替换的提示词，因此只读展示，避免运维改坏结构导致模型解析失败。
OBSERVATION_TEMPLATE_PREVIEW = (
    '[观察] 工具 {tool}，查询 "{query}"，命中 {n} 条：\n'
    "[1] 《标题》（文号）片段…\n"
    "质量信号：向量距离 …，关键词命中 …，top1 相关度 …\n"
    "提示：…（命中为空时会追加「换角度重试或基于通用知识 finish」的提醒）"
)


def export_prompts(
    tool_lines: str,
    max_rounds: int,
    prompts: dict[str, str] | None = None,
    channel: str = "qa",
    draft_templates: dict[str, str] | None = None,
) -> list[dict]:
    """导出各阶段提示词全文，供前端「提示词管理」面板查看/编辑/复制。

    返回的是**运行时实际生效的那份**（默认模板 + 已注入工具清单与收尾规则，
    或配置中心的自定义覆盖），因此面板所见与模型实际收到的一致。

    Args:
        tool_lines: 工具契约文本。
        max_rounds: 轮数硬上限；``0`` 表示不限（面板显示"不限"）。
        prompts: 配置中心的提示词覆盖值；问答通道键为 ``PROMPT_KEYS_QA``，
            起草通道为 ``PROMPT_KEYS_DRAFT``（缺省/空字符串表示使用默认）。
        channel: ``qa``（问答，默认）/ ``draft``（公文起草）。
        draft_templates: 起草通道专用——配置中心的起草提示词模板
            （``draft_system`` / ``no_materials_system``，category=prompt），
            用于"最终成稿提示词"的只读预览（该模板的编辑入口在配置中心
            「提示词模板」页，不在此面板，避免两处可改互相覆盖）。

    Returns:
        列表项字段：
        - ``key`` 配置键（保存时用）
        - ``name`` 中文名
        - ``stage`` 触发时机说明
        - ``content`` 当前生效全文
        - ``default_content`` 代码内置默认全文（供"恢复默认"预览/对比）
        - ``variables`` 可用的运行时占位符
        - ``source`` ``custom``（已自定义）/ ``default``（内置默认）/
          ``external``（在其他配置页维护，本面板只读）/ ``builtin``（只读逻辑）
        - ``editable`` 是否允许在面板编辑保存
    """
    p = prompts or {}

    def _item(
        *, key: str, name: str, stage: str, default: str, override: str,
        variables: list[str], editable: bool = True, effective: str | None = None,
        source: str | None = None,
    ) -> dict:
        custom = bool((override or "").strip())
        # effective 用于"覆盖文本需要注入运行时变量"的场景（如决策提示词）：
        # 面板必须显示**真正发给模型的那份**，否则会与实跑不一致。
        content = effective if effective is not None else (override.strip() if custom else default)
        return {
            "key": key,
            "name": name,
            "stage": stage,
            "content": content,
            "default_content": default,
            "variables": variables,
            "source": source or (("custom" if custom else "default") if editable else "builtin"),
            "editable": editable,
        }

    round_label = f"{max_rounds} 轮" if max_rounds and max_rounds > 0 else "不限轮数"

    if channel == "draft":
        # ---- 起草通道：理解/决策可配（deep_prompt_draft_*），成稿提示词
        # 复用配置中心「提示词模板」（draft_system/no_materials_system），只读预览 ----
        draft_tpl = draft_templates or {}
        final_default = draft_tpl.get("draft_system") or DEFAULT_DRAFT_SYSTEM
        final_no_material = draft_tpl.get("no_materials_system") or DEFAULT_NO_MATERIALS_SYSTEM
        decision_default = build_decision_system(
            max_rounds, tool_lines, template=DECISION_SYSTEM_TMPL_DRAFT
        )
        decision_effective = build_decision_system(
            max_rounds, tool_lines, p.get("deep_prompt_draft_decision", ""),
            template=DECISION_SYSTEM_TMPL_DRAFT,
        )
        return [
            _item(
                key="deep_prompt_draft_understand",
                name="起草需求理解",
                stage="理解阶段（每次起草 1 次，temperature=0；失败不阻断循环）",
                default=UNDERSTAND_SYSTEM_DRAFT,
                override=p.get("deep_prompt_draft_understand", ""),
                variables=[],
            ),
            _item(
                key="deep_prompt_draft_decision",
                name="起草推理决策（TAO 每轮）",
                stage=f"决策阶段（每轮 1 次；轮数策略：{round_label}）",
                default=decision_default,
                override=p.get("deep_prompt_draft_decision", ""),
                variables=["{tool_lines}", "{max_rounds}", "{round_rule}"],
                effective=decision_effective,
            ),
            _item(
                key="draft_system",
                name="最终成稿提示词（只读预览）",
                stage="循环结束后依据全部材料成稿；编辑入口：配置中心 → 提示词模板",
                default=final_default,
                override="",
                variables=[],
                editable=False,
                source="external",
            ),
            _item(
                key="no_materials_system",
                name="无材料成稿提示词（只读预览）",
                stage="未收集到任何依据时按通用公文规范成稿（不得虚构库内依据）",
                default=final_no_material,
                override="",
                variables=[],
                editable=False,
                source="external",
            ),
            _item(
                key="deep_prompt_observation",
                name="观察结果格式（只读）",
                stage="工具执行后回灌给模型的观察消息模板（结构化逻辑，不建议改）",
                default=OBSERVATION_TEMPLATE_PREVIEW,
                override="",
                variables=["{tool}", "{query}", "{n}"],
                editable=False,
            ),
        ]

    # ---- 问答通道（默认）----
    decision_default = build_decision_system(max_rounds, tool_lines)
    decision_effective = build_decision_system(
        max_rounds, tool_lines, p.get("deep_prompt_decision", "")
    )
    return [
        _item(
            key="deep_prompt_understand",
            name="提问理解",
            stage="理解阶段（每次问答 1 次，temperature=0；失败不阻断循环）",
            default=UNDERSTAND_SYSTEM,
            override=p.get("deep_prompt_understand", ""),
            variables=[],
        ),
        _item(
            key="deep_prompt_decision",
            name="推理决策（TAO 每轮）",
            stage=f"决策阶段（每轮 1 次；轮数策略：{round_label}）",
            default=decision_default,
            override=p.get("deep_prompt_decision", ""),
            variables=["{tool_lines}", "{max_rounds}", "{round_rule}"],
            effective=decision_effective,
        ),

        _item(
            key="deep_prompt_final",
            name="最终答案生成",
            stage="循环结束后综合全部材料生成答案（无材料时走通用知识兜底）",
            default=build_final_system(has_material=True) + "\n\n"
                    "【无材料分支替换为】\n" + _material_rule(False),
            override=p.get("deep_prompt_final", ""),
            variables=["{material_rule}", "{has_material}"],
        ),
        _item(
            key="deep_prompt_observation",
            name="观察结果格式（只读）",
            stage="工具执行后回灌给模型的观察消息模板（结构化逻辑，不建议改）",
            default=OBSERVATION_TEMPLATE_PREVIEW,
            override="",
            variables=["{tool}", "{query}", "{n}"],
            editable=False,
        ),
    ]

