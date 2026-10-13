"""执行器（Executor）：按计划逐步执行，逐步上报进度。

核心原则（与《Agent 意图路由与分步执行方案 V1.1》一致）：
- 每一步都**复用现有能力**：检索走 hybrid_retrieve（权限/密级/空间过滤
  原样下推），生成走 ModelProvider——Agent 是编排层，不造新引擎；
- 步骤间数据传递：后续 generate 步骤的 prompt 注入前序步骤产物摘要
  （截断 500 字/步，防上下文膨胀）；
- 每步开始/结束产出事件（step_start / step_end），由调用方转成 SSE
  推给前端——"分步可见"是复杂任务体验的核心；
- 单步失败重试 1 次，仍失败标记 failed 并**继续后续步骤**（部分失败
  好于全盘失败），最终汇总时说明"第 N 步依据不足"。

本模块为异步生成器：yield 事件字典，产物收集到 ``artifacts_out``。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.contracts import Citation, RetrieveRequest
from app.services.agent.planner import (
    ACTION_GENERATE,
    ACTION_RETRIEVE,
    Step,
)
from app.services.agent.policies import is_no_useful_info
from app.services.model_provider import ModelProvider
from app.services.retrieval import hybrid_retrieve

logger = logging.getLogger(__name__)

# 前序步骤摘要注入上限（字）：防上下文膨胀
_SUMMARY_INJECT_LIMIT = 500
# 子任务生成步的输出上限（token）
_STEP_GEN_MAX_TOKENS = 600
# 检索步的召回条数（子问题通常更聚焦，比主链路少）
_STEP_RETRIEVE_TOP_N = 4
# 单步重试次数（首次失败后重试 1 次）
_STEP_RETRY = 1


@dataclass
class StepArtifact:
    """单步执行产物。

    Attributes:
        step: 原始步骤定义（含最终 status）。
        ok: 是否成功。
        citations: 检索步命中的引用材料（供汇总合并）。
        summary: 步骤结论摘要（供后续步骤与最终汇总引用）。
        error: 失败原因（成功为空）。
    """

    step: Step
    ok: bool = False
    citations: list[Citation] = field(default_factory=list)
    summary: str = ""
    error: str = ""


async def execute_plan(
    db: AsyncSession,
    req,
    steps: list[Step],
    artifacts_out: list[StepArtifact],
) -> AsyncGenerator[dict, None]:
    """按顺序执行步骤，yield 进度事件，产物追加到 artifacts_out。

    Args:
        db: 数据库会话。
        req: 请求对象（ChatRequest/GenerateRequest 均可，读取 space_ids /
            caller_scope / max_clearance / rerank 等属性）。
        steps: 计划器产出的步骤列表。
        artifacts_out: 产物收集列表（调用方持有引用，执行结束后读取）。
    """
    for step in steps:
        step.status = "running"
        yield {
            "type": "step_start",
            "id": step.id,
            "action": step.action,
            "query": step.query,
        }
        artifact = await _run_step(db, req, step, artifacts_out)
        artifacts_out.append(artifact)
        step.status = "done" if artifact.ok else "failed"
        yield {
            "type": "step_end",
            "id": step.id,
            "ok": artifact.ok,
            "summary": artifact.summary or (artifact.error if not artifact.ok else ""),
        }


async def _run_step(
    db: AsyncSession,
    req,
    step: Step,
    prior: list[StepArtifact],
) -> StepArtifact:
    """执行单步（失败重试 _STEP_RETRY 次；仍失败返回 failed 产物，不抛出）。"""
    last_error = ""
    for attempt in range(_STEP_RETRY + 1):
        try:
            if step.action == ACTION_RETRIEVE:
                return await _run_retrieve_step(db, req, step)
            return await _run_generate_step(step, prior)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("步骤 %s 第 %d 次执行失败：%s", step.id, attempt + 1, last_error)
    return StepArtifact(step=step, ok=False, error=last_error or "未知错误")


async def _run_retrieve_step(db: AsyncSession, req, step: Step) -> StepArtifact:
    """检索步：用子问题召回素材（权限/密级/空间过滤原样下推）。

    无有用信息的检索（双信号判定）不回传引用：让汇总器走"自身能力
    兜底 + 甄别声明"路径，而不是把噪声材料当依据（用户硬性要求）。
    """
    sub_req = RetrieveRequest(
        query=step.query,
        space_ids=getattr(req, "space_ids", None),
        doc_types=getattr(req, "doc_types", None),
        top_n=_STEP_RETRIEVE_TOP_N,
        caller_scope=getattr(req, "caller_scope", []) or [],
        max_clearance=getattr(req, "max_clearance", "INTERNAL"),
        rerank=getattr(req, "rerank", True),
    )
    resp = await hybrid_retrieve(db, sub_req)
    if is_no_useful_info(resp):
        return StepArtifact(step=step, ok=True, citations=[], summary="未检索到有用信息")
    citations: list[Citation] = resp.citations
    titles = "、".join(f"《{c.title}》" for c in citations[:3])
    summary = f"命中 {len(citations)} 条相关材料（{titles}）"
    return StepArtifact(step=step, ok=True, citations=citations, summary=summary)


async def _run_generate_step(step: Step, prior: list[StepArtifact]) -> StepArtifact:
    """生成步：基于前序步骤产物完成子任务（非流式，短输出）。"""
    gen = ModelProvider(role="gen")
    messages = _build_step_messages(step, prior)
    text = await gen.generate(messages, temperature=0.3, max_tokens=_STEP_GEN_MAX_TOKENS)
    text = (text or "").strip()
    summary = text[:_SUMMARY_INJECT_LIMIT]
    return StepArtifact(step=step, ok=True, summary=text, citations=[])


def _build_step_messages(step: Step, prior: list[StepArtifact]) -> list[dict]:
    """构造生成步 messages：前序结论 + 前序检索材料 + 本步子任务。"""
    prior_lines: list[str] = []
    prior_citations: list[Citation] = []
    for art in prior:
        if art.ok and art.summary:
            prior_lines.append(f"- {art.step.query}：{art.summary[:_SUMMARY_INJECT_LIMIT]}")
        prior_citations.extend(art.citations)
    context = "\n\n".join(
        f"[{i + 1}] 《{c.title}》{('（' + c.doc_no + '）') if c.doc_no else ''}：\n{c.snippet}"
        for i, c in enumerate(prior_citations[:8])
    ) or "（前序步骤未检索到材料）"
    system = (
        "你是任务执行助手，负责完成复杂任务中的一个子任务。请基于【前序步骤结论】"
        "与【检索材料】输出本步结论，要求：结论明确、300 字以内；材料未覆盖的"
        "内容须注明'依据不足'，不得编造。"
    )
    user = (
        f"【前序步骤结论】\n{chr(10).join(prior_lines) if prior_lines else '（无）'}\n\n"
        f"【检索材料】\n{context}\n\n"
        f"【本步子任务】\n{step.query}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
