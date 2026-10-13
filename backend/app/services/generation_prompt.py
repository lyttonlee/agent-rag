"""生成提示词构造：强制引用溯源、禁止编造、按文种模板起草、多轮问答。"""

from __future__ import annotations

from app.schemas.contracts import Citation, GenerateRequest


def build_generation_messages(req: GenerateRequest, citations: list[Citation]) -> list[dict]:
    system = (
        "你是企业公文辅助写作引擎。请基于【检索材料】回答，并遵守：\n"
        "1. 所有事实性陈述必须标注引用，格式 [n] 对应材料序号；\n"
        "2. 检索材料未覆盖的内容，必须明确说明'依据不足，建议核实'，不得编造；\n"
        "3. 用语符合公文规范，结构清晰。\n"
    )
    if req.template_doc_type:
        system += f"4. 按「{req.template_doc_type}」文种格式起草。\n"

    context = "\n\n".join(
        f"[{i+1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{c.snippet}"
        for i, c in enumerate(citations)
    ) or "（无可用检索材料）"

    user = f"【检索材料】\n{context}\n\n【用户问题】\n{req.query}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_chat_messages(
    query: str,
    history: list[tuple[str, str]],
    citations: list[Citation],
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
    """
    system = (
        "你是企业公文知识库的智能问答助手。请基于【检索材料】与【对话历史】回答，并遵守：\n"
        "1. 所有事实性陈述必须标注引用，格式 [n] 对应本轮检索材料序号；\n"
        "2. 检索材料未覆盖的内容，必须明确说明'依据不足，建议核实'，不得编造；\n"
        "3. 结合对话历史理解用户指代（如'它''该文件''上述要求'），保持上下文连贯；\n"
        "4. 用语简洁专业，结构清晰；与公文起草无关时不要套用公文格式。\n"
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    # 历史轮次（content 截断，防止早期超长答案挤占上下文窗口）
    for role, content in history:
        messages.append({"role": role, "content": content[:2000]})
    context = "\n\n".join(
        f"[{i+1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{c.snippet}"
        for i, c in enumerate(citations)
    ) or "（无可用检索材料）"
    messages.append({
        "role": "user",
        "content": f"【检索材料】\n{context}\n\n【用户问题】\n{query}",
    })
    return messages
