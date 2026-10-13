"""汇总器（Aggregator）：把各步骤产物综合为最终答案的 messages 与引用。

职责边界：
- 只负责"拼装"（messages + 合并去重引用 + 失败步骤声明），不负责流式
  输出与落库——那些由调用方（services/chat.py）统一处理；
- 无材料模式：所有步骤都没命中材料时，切换为"模型自身能力完成"提示词，
  并由调用方在答案末尾追加甄别声明（TASK_DISCLAIMER）；
- 引用角标规则与现有链路一致（[n] 对应材料序号）。
"""

from __future__ import annotations

from app.schemas.contracts import Citation
from app.services.agent.executor import StepArtifact

# 步骤产物注入上限（字/步）：与执行器保持一致，防上下文膨胀
_ARTIFACT_INJECT_LIMIT = 500
# 汇总引用的材料条数上限（防 prompt 过长）
_MAX_AGG_CITATIONS = 8


def merge_citations(artifacts: list[StepArtifact]) -> list[Citation]:
    """合并各步骤引用（按 chunk_id 去重，保持首次出现顺序）。"""
    seen: set[str] = set()
    merged: list[Citation] = []
    for art in artifacts:
        for c in art.citations:
            if c.chunk_id in seen:
                continue
            seen.add(c.chunk_id)
            merged.append(c)
    return merged[:_MAX_AGG_CITATIONS]


def has_materials(artifacts: list[StepArtifact]) -> bool:
    """是否存在任何有效检索材料。"""
    return any(art.citations for art in artifacts)


def failed_step_ids(artifacts: list[StepArtifact]) -> list[str]:
    """失败步骤 ID 列表（汇总时向用户声明依据不足）。"""
    return [art.step.id for art in artifacts if not art.ok]


def build_final_messages(
    query: str,
    history: list[tuple[str, str]],
    artifacts: list[StepArtifact],
    citations: list[Citation],
) -> list[dict]:
    """构造汇总阶段 messages。

    Args:
        query: 用户原始问题。
        history: 多轮历史 [(role, content), ...]。
        artifacts: 各步骤产物（含失败步骤，用于声明依据不足）。
        citations: 合并去重后的引用材料。
    """
    no_materials = not citations
    failed = failed_step_ids(artifacts)

    if no_materials:
        # 无材料模式：改用模型自身能力完成（任务型意图专属），
        # 提示词明确要求不得虚构知识库依据，答案由调用方追加甄别声明。
        system = (
            "你是企业公文知识库的任务执行助手。本次执行**未检索到知识库材料**，"
            "请基于你的通用能力完成用户的任务，并遵守：\n"
            "1. 不得声称引用了知识库文档或内部规定（没有材料可引）；\n"
            "2. 通用性表述须严谨，涉及具体数据/规定时明确说明'需以实际情况为准'；\n"
            "3. 结合对话历史理解用户指代，保持上下文连贯；\n"
            "4. 结构清晰、结论明确。\n"
        )
    else:
        system = (
            "你是企业公文知识库的任务执行助手。请基于【步骤产物】与【检索材料】"
            "完成用户的复杂任务，并遵守：\n"
            "1. 事实性陈述标注引用 [n] 对应材料序号；\n"
            "2. 材料未覆盖的内容须注明'依据不足'，不得编造；\n"
            "3. 结合对话历史理解用户指代，保持上下文连贯；\n"
            "4. 结构清晰、结论明确。\n"
        )
    if failed:
        system += f"5. 注意：步骤 {'、'.join(failed)} 执行失败，其依据不足，汇总时须如实说明。\n"

    messages: list[dict] = [{"role": "system", "content": system}]
    for role, content in history:
        messages.append({"role": role, "content": content[:2000]})

    artifact_lines: list[str] = []
    for art in artifacts:
        if not art.ok:
            artifact_lines.append(f"- 【失败】{art.step.query}（{art.error or '执行失败'}）")
        elif art.summary:
            artifact_lines.append(f"- {art.step.query}：{art.summary[:_ARTIFACT_INJECT_LIMIT]}")
    context = "\n\n".join(
        f"[{i + 1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{c.snippet}"
        for i, c in enumerate(citations)
    )
    user = (
        f"【步骤产物】\n{chr(10).join(artifact_lines) if artifact_lines else '（无）'}\n\n"
        f"【检索材料】\n{context if not no_materials else '（未检索到知识库材料）'}\n\n"
        f"【用户任务】\n{query}"
    )
    messages.append({"role": "user", "content": user})
    return messages
