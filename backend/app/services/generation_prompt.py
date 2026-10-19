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


# ---- 多轮修改（用户 2026-10-16 硬性要求：同一会话内再次提修改意见时，
# 必须把往轮对话注入上下文，否则模型只看到本轮 query + 检索材料，会重新
# 生成一份完整文稿而不是在上一稿基础上修改）----
# 历史消息截断上限：往轮草稿（assistant）是修改基准，阈值放宽；用户修改
# 意见（user）从紧。防止早期超长草稿挤占上下文窗口。
_DRAFT_HISTORY_USER_MAX = 1000
_DRAFT_HISTORY_ASSISTANT_MAX = 6000

# 多轮修订规则：独立于可配置模板追加——运维自定义 draft_system 后，
# "在上一稿基础上修改、输出完整文稿"的规则依然生效（配置只覆盖正文指令）。
_DRAFT_REVISION_RULE = (
    "【多轮修改】对话历史中包含本轮之前的用户要求与往轮草稿。"
    "若历史中已有草稿：本轮请求是对上一稿的修改——请以上一稿为基础进行修改，"
    "输出完整的修改后文稿（不要只描述改动或输出片段，未涉及部分保留原文），"
    "文种格式与上一稿保持一致。\n"
)


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
    history: list[tuple[str, str]] | None = None,
    process: str = "",
    reference_instructions: str = "",
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
        history: 会话历史 [(role, content), ...]（按时间正序，已排除本轮
            user 消息）——同一会话内再次提修改意见时注入往轮要求与上稿，
            使模型"在上一稿基础上修改"而非重新起草（用户 2026-10-16 需求）。
        process: 深度推理（ReAct）过程的文字摘要（2026-10-19 起草 ReAct 化）。
            智能体在循环里自主收集了哪些依据、为什么收尾，透传给成稿模型，
            让它知道"我的同事已经查过什么"，避免重复劳动与依据遗漏；
            单趟链路传空串（不影响旧行为）。
        reference_instructions: 上传参考文档的指令块（2026-10-19，由
            services/reference_profile.render_reference_instructions 预渲染）：
            按参考文档的模板格式生成和/或仿写其结构行文；空串=无参考文档。
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
    # 多轮修改：有历史（往轮草稿）时追加修订规则（独立于可配置模板）
    if history:
        system += _DRAFT_REVISION_RULE
    # 上传参考文档（2026-10-19）：按参考文档的模板格式生成 / 仿写。
    # 指令由 services/reference_profile.render_reference_instructions 预渲染，
    # 此处只做拼装——提示词模块不依赖画像结构，便于单测与后续调整措辞。
    reference_block = (
        f"\n【参考文档要求】\n{reference_instructions}\n"
        if (reference_instructions or "").strip()
        else ""
    )
    if reference_block:
        system += reference_block

    messages: list[dict] = [{"role": "system", "content": system}]
    # 历史轮次（assistant=往轮草稿放宽截断，user=修改意见从紧，见常量注释）
    for role, content in (history or []):
        cap = _DRAFT_HISTORY_ASSISTANT_MAX if role == "assistant" else _DRAFT_HISTORY_USER_MAX
        messages.append({"role": role, "content": content[:cap]})
    process_block = f"【推理过程】\n{process}\n\n" if (process or "").strip() else ""
    user = (
        f"{process_block}"
        f"【检索材料】\n{_render_context(citations)}\n\n【用户问题】\n{req.query}"
    )
    messages.append({"role": "user", "content": user})
    return messages


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
