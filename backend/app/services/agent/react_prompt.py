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
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.schemas.contracts import Citation

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

# ---- 决策 system prompt（TAO 循环每轮）----
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
    "6. 信息足够就 finish，不要为问而问；本轮上限 {max_rounds} 轮。"
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


def build_decision_system(max_rounds: int, tool_lines: str) -> str:
    """组装决策 system prompt（工具清单与轮数由配置注入）。"""
    return DECISION_SYSTEM_TMPL.format(tool_lines=tool_lines, max_rounds=max_rounds)


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


def build_final_messages(
    query: str,
    history: list[tuple[str, str]],
    citations: list[Citation],
    rounds_log: list[dict],
) -> list[dict]:
    """最终生成 messages：多轮材料综合 + 交叉验证 + 全局引用编号。

    citations 已由策略层按相关度去重排序并重编号 [1..N]。
    """
    has_material = bool(citations)
    if has_material:
        material_rule = (
            "1. 事实性陈述必须标注引用 [n]（对应材料序号）；\n"
            "2. 材料相互矛盾时并列指出，并说明通常以更新发布的规定/上级规定为准；\n"
            "3. 材料未覆盖的内容注明「依据不足」，可以结合通用公文知识补全并说明；\n"
        )
    else:
        material_rule = (
            "1. **本次未检索到可引用的知识库材料**。你必须基于自身通用公文知识"
            "**完成对用户诉求的完整回答**，不得拒答、不得只回一句「无法回答」；\n"
            "2. 对无法确认的部分明确标注「以下为通用规范建议，知识库暂无对应规定，"
            "请以实际收到的正式文件为准」；\n"
        )
    system = (
        "你是企业公文知识库的深度推理问答助手。以下【引用材料】"
        + ("来自多轮检索综合，" if has_material else "本次为空，")
        + "请完成对用户诉求的回答，并遵守：\n"
        + material_rule
        + "4. 结合对话历史理解用户指代，保持上下文连贯；\n"
        + "5. 结构清晰、结论明确、直接给出可执行结果，不要反问用户；\n"
        + "6. 系统会在你的回答末尾统一附加「AI 生成，请人工鉴别」声明，你无需自己写。"
    )
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
def export_prompts(tool_lines: str, max_rounds: int) -> list[dict]:
    """导出各阶段提示词全文，供前端提示词管理面板查看/复制。

    运维在配置中心或本面板核对"模型到底被怎么指示的"，无需翻代码。
    返回 [{key, name, stage, content, variables}]。
    """
    return [
        {
            "key": "deep_prompt_understand",
            "name": "提问理解",
            "stage": "理解阶段（每轮问答 1 次，temperature=0）",
            "content": UNDERSTAND_SYSTEM,
            "variables": [],
        },
        {
            "key": "deep_prompt_decision",
            "name": "推理决策（TAO 每轮）",
            "stage": f"决策阶段（每轮 1 次，当前档位上限 {max_rounds} 轮）",
            "content": build_decision_system(max_rounds, tool_lines),
            "variables": ["{tool_lines}", "{max_rounds}"],
        },
        {
            "key": "deep_prompt_observation",
            "name": "观察结果格式",
            "stage": "工具执行后回灌给模型的观察消息模板",
            "content": (
                '[观察] 工具 {tool}，查询 "{query}"，命中 {n} 条：\n'
                "[i] 《标题》（文号）片段…\n质量信号：…\n提示：…"
            ),
            "variables": ["{tool}", "{query}", "{n}"],
        },
        {
            "key": "deep_prompt_final",
            "name": "最终答案生成",
            "stage": "循环结束后综合全部材料生成答案（无材料时走通用知识兜底）",
            "content": (
                "你是企业公文知识库的深度推理问答助手……\n"
                "1. 有材料：事实性陈述必须标注引用 [n]；\n"
                "2. 无材料：基于通用公文知识完成回答，不得拒答，标注「知识库暂无对应规定」；\n"
                "6. 系统会在末尾统一附加「AI 生成，请人工鉴别」声明。"
            ),
            "variables": [],
        },
    ]
