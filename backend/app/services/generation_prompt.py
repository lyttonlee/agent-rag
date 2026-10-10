"""生成提示词构造：强制引用溯源、禁止编造、按文种模板起草。"""

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
