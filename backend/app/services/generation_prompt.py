"""生成提示词构造：强制引用溯源、禁止编造、按文种模板起草、多轮问答。

模板可配置化（用户 2026-10-13 需求）：三类 system 提示词
（公文起草 / 智能问答 / 无材料兜底）的默认文案即下方常量，
运维可在「配置中心 → 提示词模板」编辑（category=prompt），
保存后由调用方经 ``templates`` 参数注入；缺项回退默认文案。
"""

from __future__ import annotations

from app.schemas.contracts import Citation, GenerateRequest

# ---- 默认提示词模板（与配置中心 category=prompt 的键一一对应）----
# 配置中心编辑后存 ConfigKV.value = {"value": "<模板文本>"}，运行时覆盖本默认值。
DEFAULT_DRAFT_SYSTEM = (
    "你是企业公文辅助写作引擎。请基于【检索材料】回答，并遵守：\n"
    "1. 所有事实性陈述必须标注引用，格式 [n] 对应材料序号；\n"
    "2. 检索材料未覆盖的内容，必须明确说明'依据不足，建议核实'，不得编造；\n"
    "3. 用语符合公文规范，结构清晰。\n"
)
DEFAULT_NO_MATERIALS_SYSTEM = (
    "你是企业公文辅助写作引擎。本次未检索到知识库参考材料，请基于你的"
    "通用公文写作能力完成起草，并遵守：\n"
    "1. 不得声称引用了知识库文档、内部规定或具体文号（没有材料可引）；\n"
    "2. 涉及具体数据、时限、责任人等处使用占位符或说明'需按实际情况填写'；\n"
    "3. 用语符合公文规范，结构清晰。\n"
)
DEFAULT_CHAT_SYSTEM = (
    "你是企业公文知识库的智能问答助手。请基于【检索材料】与【对话历史】回答，并遵守：\n"
    "1. 所有事实性陈述必须标注引用，格式 [n] 对应本轮检索材料序号；\n"
    "2. 检索材料未覆盖的内容，必须明确说明'依据不足，建议核实'，不得编造；\n"
    "3. 结合对话历史理解用户指代（如'它''该文件''上述要求'），保持上下文连贯；\n"
    "4. 用语简洁专业，结构清晰；与公文起草无关时不要套用公文格式。\n"
)
DEFAULT_CHAT_NO_MATERIALS_SYSTEM = (
    "你是企业公文知识库的智能问答助手。本次未检索到知识库参考材料，"
    "但用户的请求属于完成任务型（如起草、创作、整理），请基于你的"
    "通用能力直接完成，并遵守：\n"
    "1. 不得声称引用了知识库文档、内部规定或具体文号（没有材料可引）；\n"
    "2. 涉及具体数据、时限、责任人等处使用占位符或说明'需按实际情况填写'；\n"
    "3. 结合对话历史理解用户指代，保持上下文连贯；\n"
    "4. 用语简洁专业，结构清晰。\n"
)


def _pick(templates: dict[str, str] | None, key: str, default: str) -> str:
    """从配置中心模板中取值；缺项/空值回退默认文案。"""
    if templates:
        val = templates.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return default


def _render_context(citations: list[Citation]) -> str:
    """渲染检索材料上下文。

    父块注入的大块（granularity='large'）携带完整正文 content——生成时
    必须用它而非 200 字 snippet，否则"补上下文"的注入失去意义；
    小块仍用 snippet（与旧行为一致，控制上下文体积）。
    """
    if not citations:
        return "（无可用检索材料）"
    parts = []
    for i, c in enumerate(citations):
        body = c.content if (c.granularity == "large" and c.content) else c.snippet
        parts.append(
            f"[{i+1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{body}"
        )
    return "\n\n".join(parts)


def build_generation_messages(
    req: GenerateRequest,
    citations: list[Citation],
    no_materials: bool = False,
    templates: dict[str, str] | None = None,
    template_name: str | None = None,
) -> list[dict]:
    """构造公文起草 messages。

    Args:
        req: 起草请求（query / template_doc_type / template_code）。
        citations: 检索引用材料（可含父块注入的大块，见 _render_context）。
        no_materials: 未检索到材料时的"自身能力起草"模式——提示词切换为
            通用写作能力指令，并明确不得虚构知识库依据（答案由调用方追加
            甄别声明，见 agent.policies.TASK_DISCLAIMER）。
        templates: 配置中心提示词模板（{key: 文本}，key ∈ draft_system /
            no_materials_system / chat_system / chat_no_materials_system）；
            None 或缺键时用代码默认文案。
        template_name: 具体模板名称（同一文种可配置多模板，用户选定后传入）；
            用于在文种指令中带出"模板：XXX"，让生成更贴合所选模板。
    """
    system = _pick(
        templates,
        "no_materials_system" if no_materials else "draft_system",
        DEFAULT_NO_MATERIALS_SYSTEM if no_materials else DEFAULT_DRAFT_SYSTEM,
    )
    if req.template_doc_type:
        line = f"4. 按「{req.template_doc_type}」文种格式起草。"
        if template_name:
            line += f"（采用模板：{template_name}）"
        line += "\n"
        system += line

    user = f"【检索材料】\n{_render_context(citations)}\n\n【用户问题】\n{req.query}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_chat_messages(
    query: str,
    history: list[tuple[str, str]],
    citations: list[Citation],
    no_materials: bool = False,
    templates: dict[str, str] | None = None,
) -> list[dict]:
    """多轮智能问答的 messages 构造。

    与单轮起草的差异：
    - system 增加"结合对话历史理解指代"的指令（多轮追问常省略主语）；
    - 历史消息以 user/assistant 交替注入（最近 N 轮，见 services/conversation.py）；
    - 本轮 user 消息携带【检索材料】+ 当前问题，引用角标规则同单轮。

    Args:
        query: 本轮用户问题（检索也以此为 query，避免历史稀释召回精度）。
        history: [(role, content), ...] 历史消息，按时间正序。
        citations: 本轮检索命中的引用材料。
        no_materials: 任务型意图（起草/创作/完成任务）未检索到材料时的
            "自身能力完成"模式——提示词切换为通用能力指令，明确不得
            声称引用知识库、数据处用占位符（答案由调用方追加甄别声明，
            见 agent.policies.TASK_DISCLAIMER）。确切回答型意图不使用
            本模式（无材料必须拒答，见 chat.py 策略分流）。
        templates: 配置中心提示词模板；None 或缺键时用代码默认文案。
    """
    system = _pick(
        templates,
        "chat_no_materials_system" if no_materials else "chat_system",
        DEFAULT_CHAT_NO_MATERIALS_SYSTEM if no_materials else DEFAULT_CHAT_SYSTEM,
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    # 历史轮次（content 截断，防止早期超长答案挤占上下文窗口）
    for role, content in history:
        messages.append({"role": role, "content": content[:2000]})
    messages.append({
        "role": "user",
        "content": f"【检索材料】\n{_render_context(citations)}\n\n【用户问题】\n{query}",
    })
    return messages
