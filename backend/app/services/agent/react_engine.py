"""ReAct 引擎：TAO 主循环（Thought → Action → Observation）+ 护栏 + SSE 事件。

职责边界：
- 本模块是**异步生成器**：yield SSE 事件字典（由调用方转成 SSE 文本），
  检索结论写入 ``ReactRunResult``（调用方持有引用，循环结束后读取）；
- 不做最终答案生成（那是策略层的事，复用 generate_stream 流式链路）；
- 不感知 HTTP/会话/落库——纯编排，可完整单测。

核心设计红线（2026-10-16 用户明确要求）：
- **工具调用权完全归模型**：本模块不包含"要不要检索""要不要看文档"的任何
  路由或前置判断。第一轮就是决策轮，模型可以直接 finish（基于通用知识
  作答），也可以先检索再 read_document 核对原文。
- **无降级**：理解阶段（UNDERSTAND_SYSTEM）失败或超时**不阻断循环**，只是
  少一段参考信息；不存在"单跳直接检索"短路分支。
- **不拒答**：材料不足不是终止条件，观察为空时会明确提示模型"换关键词或
  基于通用知识 finish 完成作答"。最终答案由策略层生成（无材料走通用知识
  兜底 + 末尾 AI 声明），引擎不做拒答判断。
- **多轮上下文**：会话历史注入决策历史，模型能理解"它""上一步说的"等指代。

护栏（防模型决策退化，任何一条触发即收敛）：
1. 轮数上限：**默认不限**（``cfg.max_rounds=0``，2026-10-19 用户要求）；
   运维可设正数作为硬闸，仅在"模型持续空转"的极端场景下兜底；
2. 全局时间预算：**默认不限**（``cfg.budget_ms=0``），可按需设上限；
3. 决策调用失败：连续 2 次退出（第 1 次注入重试提示继续）；
4. 决策输出不可解析：连续 2 次退出（第 1 次回灌纠错提示）；
5. query 去重：归一化相同的检索跳过真实工具调用（观察标注跳过）；
6. **无进展强制收尾**：连续 ``cfg.no_progress_limit`` 轮无新引用 → 收尾
   （先在第 N-1 轮注入"换角度或收尾"提示，再触发退出）。这是取消轮数
   上限后**替代性的收敛保障**——不限轮 ≠ 允许无限空转；
7. 未知工具：不执行，观察提示可用工具清单。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.contracts import Citation
from app.services.agent.react_config import ReactConfig
from app.services.agent.react_prompt import (
    TOOL_FINISH,
    AnalysisResult,
    build_decision_system,
    build_trace_text,
    format_observation,
    parse_analysis,
    parse_decision,
)
from app.services.agent.react_tools import (
    TOOL_DESCRIPTIONS,
    ToolResult,
    execute_tool,
    tool_lines,
    tool_names,
)
from app.services.model_provider import ModelProvider

logger = logging.getLogger(__name__)

# 连续失败容忍上限（决策调用 / JSON 解析各自计数）
_MAX_CONSECUTIVE_FAIL = 2

# 停止原因 → 中文标签（SSE react_end 与落库 trace 共用）
STOP_REASON_LABELS: dict[str, str] = {
    "final": "信息充分（模型主动结束）",
    "max_rounds": "达到轮数上限",
    "budget": "超出时间预算",
    "no_progress": "连续多轮无新材料，已强制收尾",
    "parse_fail": "决策输出连续不可解析",
    "decision_fail": "决策调用连续失败",
}

# query 归一化：去空白与常见标点后小写（重复检索判定用）
_PUNCT_RE = re.compile(r"[\s,，。.、;；:：?？!！\"'‘’“”\"'()（）\[\]【】]+")

# 工具名 → 阶段播报用的中文动作名（前端显示"正在检索…"）
_TOOL_PHASE_LABEL: dict[str, str] = {
    "knowledge_search": "检索知识库",
    "doc_lookup": "查找候选文件",
    "read_document": "打开原文",
}


@dataclass
class ReactRunResult:
    """一次 ReAct 运行的结论（调用方在生成器结束后读取）。"""

    citations: list[Citation] = field(default_factory=list)  # 全轮合并去重后的引用
    rounds: int = 0
    # 默认 final：不限轮数（V1.2 起）时正常出口是模型主动 finish，
    # 只有运维显式设了轮数硬闸才可能是 max_rounds。
    stop_reason: str = "final"
    analysis: AnalysisResult = field(default_factory=AnalysisResult)
    rounds_log: list[dict] = field(default_factory=list)     # 每轮结构化记录（trace/前端回放）
    trace_text: str = ""                                      # TAO 全链文本（随消息落库）


def _normalize_query(q: str) -> str:
    """query 归一化（去标点空白 + 小写），用于重复检索判定。"""
    return _PUNCT_RE.sub("", q or "").lower()


def _merge_citations(existing: list[Citation], new_hits: list[Citation], cap: int) -> int:
    """把新命中并入累计引用（按 chunk_id 去重保留先到者），返回新增条数。"""
    seen = {c.chunk_id for c in existing}
    added = 0
    for c in new_hits:
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        existing.append(c)
        added += 1
        if len(existing) >= cap:
            break
    return added


def build_history_hint(history: list[tuple[str, str]] | None, limit: int = 6) -> str:
    """把会话历史转成决策上下文的提示文本（多轮指代消解）。

    只取最近 limit 条（策略层已按 _HISTORY_LIMIT 取过），assistant 回答
    截断到 500 字——决策阶段只需知道"前面聊过什么"，不需要完整答案。
    """
    if not history:
        return ""
    rows = history[-limit:]
    lines = []
    for role, content in rows:
        who = "用户" if role == "user" else "我"
        text = (content or "").strip().replace("\n", " ")
        if len(text) > 500:
            text = text[:500] + "…"
        if text:
            lines.append(f"{who}：{text}")
    if not lines:
        return ""
    return "【本次会话历史（用于理解指代与上下文）】\n" + "\n".join(lines) + "\n\n"


async def run_react(
    db: AsyncSession,
    req,
    cfg: ReactConfig,
    result: ReactRunResult,
    caller=None,
    history: list[tuple[str, str]] | None = None,
) -> AsyncGenerator[dict, None]:
    """ReAct 主循环：yield SSE 事件字典，结论写入 result。

    Args:
        db: 数据库会话（工具执行用）。
        req: 问答请求（读取 space_ids/doc_types/caller_scope/max_clearance）。
        cfg: **已按档位派生**的生效配置（调用方先做 ``cfg.for_mode(mode)``），
            其中 ``max_rounds=0`` 表示不限轮数、``budget_ms=0`` 表示不限时长。
        result: 结论收集器（调用方持有引用）。
        caller: 调用方（doc_lookup/read_document 空间可见性用；可为 None）。
        history: 会话历史 [(role, content)]，注入决策上下文支持多轮追问。
    """
    started = time.monotonic()
    gen = ModelProvider(role="gen")
    # 轮数策略：0 = 不限（默认）。这里只做展示/文本注入，不做硬性判断，
    # 真正的硬闸在循环里用 `max_rounds > 0` 判断——0 是"不限"而非"零轮"。
    max_rounds = cfg.max_rounds
    budget_ms = cfg.budget_ms

    # ---- 阶段播报（2026-10-16 新增，解决"页面像卡住了"）----
    # 背景：模型每次调用（理解/决策/生成）要 2-4 秒，此前这段完全静默——
    # 前端只有收到 react_start 才知道在动，而 react_start 之前还有意图分类的
    # 沉默期，用户看到的就是"什么都没发生"。这里在**每个耗时动作前后**播报阶段，
    # 让页面持续有可见进展（配前端 elapsed 计时器可显示"已思考 Ns"）。
    yield {
        "type": "react_start",
        "mode": "react",
        "max_rounds": max_rounds,
        # 显式标记"不限轮数"，避免前端把 0 显示成"0 轮"
        "unlimited_rounds": max_rounds <= 0,
        "total_budget_ms": budget_ms,
        "tools": tool_names(),
    }
    yield {"type": "phase", "phase": "understand", "label": "正在理解你的问题…"}

    # ---- 提问理解（轻量一次调用；**失败不阻断**，仅少一段参考）----
    # 注意：此处不做任何"单跳/多跳"路由，仅让模型复述诉求并自述是否需要
    # 查库，结果注入决策历史供参考——动作决定权仍在每轮决策里。
    # 提示词可被配置中心整段覆盖（cfg.prompt_understand，空=内置默认）。
    analysis = AnalysisResult()
    try:
        raw = await asyncio.wait_for(
            gen.generate(
                [
                    {"role": "system", "content": (cfg.prompt_understand or "").strip() or _UNDERSTAND_SYSTEM},
                    {"role": "user", "content": req.query[:500]},
                ],
                temperature=0,
                max_tokens=400,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            timeout=cfg.analyze_timeout_ms / 1000,
        )

        parsed = parse_analysis(raw)
        if parsed is not None:
            analysis = parsed
        else:
            analysis = AnalysisResult(restate="（理解输出不可解析，直接进入推理）", needs_knowledge=True)
    except Exception as exc:
        logger.info("提问理解失败（不阻断，直接进入推理循环）：%s", exc)
        analysis = AnalysisResult(restate="（理解阶段跳过，直接进入推理）", needs_knowledge=True)
    result.analysis = analysis
    yield {
        "type": "analysis",
        "restate": analysis.restate,
        "needs_knowledge": analysis.needs_knowledge,
        "needs_knowledge_reason": analysis.needs_knowledge_reason,
        "candidates": analysis.candidates,
        "ambiguities": analysis.ambiguities,
    }

    # ---- 决策历史（system + **用户问题与历史** + 每轮 assistant 决策 / user 观察）----
    # 红线：首条 user 消息必须含原始问题——决策模型不知道"用户在问什么"
    # 就会回「用户尚未提出具体问题」直接 finish（真实端到端踩坑）。
    # 多轮：会话历史一并注入，"它/那份文件/上一步"等指代才能正确解析。
    first_hint = build_history_hint(history) + f"【用户问题】\n{req.query}\n\n"
    first_hint += analysis.as_hint_text() + "\n\n"
    first_hint += "请开始：输出一个 JSON 决策（thought + action）。" \
                  "你可以选择 knowledge_search / doc_lookup / read_document，" \
                  "也可以判断无需查库直接 finish。"
    history_msgs: list[dict] = [
        {
            "role": "system",
            "content": build_decision_system(max_rounds, tool_lines(), cfg.prompt_decision),
        },
        {"role": "user", "content": first_hint},
    ]
    seen_queries: set[str] = set()
    parse_fail = 0
    decision_fail = 0
    no_progress = 0
    # 默认停止原因：只有设置了轮数硬上限时才可能是 max_rounds，
    # 不限轮数时正常退出路径是 final / no_progress / *_fail。
    result.stop_reason = "final"

    # ---- TAO 主循环：**不限轮数**（默认），由模型自主决定何时 finish ----
    # 用 while 而非 for range：``max_rounds=0`` 是"不限"的意思，不能被
    # range(1, 1) 误判成 0 轮直接退出。硬上限仅在运维显式设置正数时生效。
    round_no = 0
    while True:
        round_no += 1
        if max_rounds and max_rounds > 0 and round_no > max_rounds:
            result.stop_reason = "max_rounds"
            logger.info("ReAct 达到轮数上限 %d，收敛", max_rounds)
            break
        result.rounds = round_no
        round_started = time.monotonic()
        # 预算闸口：每轮进入前检查（**仅在设置了预算时**；默认不限）
        if budget_ms and budget_ms > 0 and (time.monotonic() - started) * 1000 > budget_ms:
            result.stop_reason = "budget"
            logger.info("ReAct 预算耗尽（%.0fms>%dms），收敛", (time.monotonic() - started) * 1000, budget_ms)
            break
        yield {
            "type": "react_round",
            "round": round_no,
            "max_rounds": max_rounds,
            "unlimited_rounds": max_rounds <= 0,
        }

        # 阶段播报：本轮模型正在推理决策（这段通常 1-4 秒，此前页面无任何提示）
        yield {
            "type": "phase", "phase": "decide", "round": round_no,
            "label": f"第 {round_no} 轮：正在思考下一步该做什么…",
        }

        # ---- 决策调用（严格单 JSON 输出；关思考链防解析干扰）----
        try:
            raw = await asyncio.wait_for(
                gen.generate(
                    history_msgs,
                    temperature=cfg.temperature,
                    max_tokens=512,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                ),
                timeout=cfg.decision_timeout_ms / 1000,
            )
        except Exception as exc:
            decision_fail += 1
            logger.warning("ReAct 决策调用失败（第 %d 次）：%s", decision_fail, exc)
            result.rounds_log.append({
                "round": round_no, "thought": "", "action": None,
                "observation_hits": 0,
                "observation_note": f"决策调用失败（{decision_fail}/{_MAX_CONSECUTIVE_FAIL}）",
                "elapsed_ms": int((time.monotonic() - round_started) * 1000),
            })
            if decision_fail >= _MAX_CONSECUTIVE_FAIL:
                result.stop_reason = "decision_fail"
                break
            history_msgs.append({"role": "user", "content": "[系统提示] 上次请求未能完成，请重新输出一个 JSON 决策。"})
            continue

        decision = parse_decision(raw)
        if decision is None:
            parse_fail += 1
            logger.info("ReAct 决策输出不可解析（第 %d 次）：%.120s", parse_fail, raw or "")
            result.rounds_log.append({
                "round": round_no, "thought": "", "action": None,
                "observation_hits": 0,
                "observation_note": f"输出不可解析（{parse_fail}/{_MAX_CONSECUTIVE_FAIL}）",
                "elapsed_ms": int((time.monotonic() - round_started) * 1000),
            })
            if parse_fail >= _MAX_CONSECUTIVE_FAIL:
                result.stop_reason = "parse_fail"
                break
            # 回灌纠错：让模型看到自己的错误输出并重试（不整体失败）
            history_msgs.append({"role": "assistant", "content": (raw or "")[:500]})
            history_msgs.append({
                "role": "user",
                "content": "[系统提示] 你的上次输出不是合法 JSON。请严格只输出一个 JSON 对象（含 thought 与 action）。",
            })
            continue
        # 一次成功即清零失败计数（避免历史失败累积误杀）
        parse_fail = 0
        decision_fail = 0

        yield {"type": "thought", "round": round_no, "text": decision.thought}

        # ---- finish：模型自主决定信息已足（或可基于通用知识作答），进入最终生成 ----
        if decision.tool == TOOL_FINISH:
            result.stop_reason = "final"
            result.rounds_log.append({
                "round": round_no, "thought": decision.thought,
                "action": {"tool": TOOL_FINISH, "query": "", "skipped": False},
                "observation_hits": 0, "observation_note": "",
                "elapsed_ms": int((time.monotonic() - round_started) * 1000),
            })
            break

        # ---- 未知工具：不执行，观察提示 ----
        if decision.tool not in TOOL_DESCRIPTIONS:
            note = f"未知工具 {decision.tool}，可用：{'、'.join(tool_names())}"
            yield {"type": "action", "round": round_no, "tool": decision.tool,
                   "query": decision.query, "skipped": True}
            yield {"type": "observation", "round": round_no, "tool": decision.tool,
                   "query": decision.query, "hits": [], "quality": {}, "note": note}
            history_msgs.append({"role": "assistant", "content": raw})
            history_msgs.append({"role": "user", "content": f"[观察] {note}"})
            result.rounds_log.append({
                "round": round_no, "thought": decision.thought,
                "action": {"tool": decision.tool, "query": decision.query, "skipped": True},
                "observation_hits": 0, "observation_note": note,
                "elapsed_ms": int((time.monotonic() - round_started) * 1000),
            })
            continue

        # ---- query 去重 / 空 query：跳过真实工具调用 ----
        norm = _normalize_query(decision.query)
        if not norm:
            note = "缺少 query 参数，未执行工具调用"
            skipped = True
        elif norm in seen_queries:
            note = "重复调用已跳过，请更换关键词或基于现有材料 finish"
            skipped = True
        else:
            note = ""
            skipped = False
            seen_queries.add(norm)
        if skipped:
            yield {"type": "action", "round": round_no, "tool": decision.tool,
                   "query": decision.query, "skipped": True}
            yield {"type": "observation", "round": round_no, "tool": decision.tool,
                   "query": decision.query, "hits": [], "quality": {}, "note": note}
            history_msgs.append({"role": "assistant", "content": raw})
            history_msgs.append({"role": "user", "content": f"[观察] {note}"})
            result.rounds_log.append({
                "round": round_no, "thought": decision.thought,
                "action": {"tool": decision.tool, "query": decision.query, "skipped": True},
                "observation_hits": 0, "observation_note": note,
                "elapsed_ms": int((time.monotonic() - round_started) * 1000),
            })
            no_progress += 1
        else:
            # 阶段播报：工具执行（检索/rerank 可能 3-8 秒）
            yield {
                "type": "phase", "phase": "tool", "round": round_no,
                "tool": decision.tool,
                "label": f"第 {round_no} 轮：正在执行 {_TOOL_PHASE_LABEL.get(decision.tool, decision.tool)}「{decision.query}」…",
            }
            yield {"type": "action", "round": round_no, "tool": decision.tool,
                   "query": decision.query, "skipped": False}
            # 工具执行：编排层再兜一层异常（工具层已内部捕获；此处防未来
            # 新注册工具实现漏捕获——异常一律转为观察 note，模型可换角度）
            try:
                tr: ToolResult = await execute_tool(
                    db, req, decision.tool, {"query": decision.query}, cfg, caller
                )
            except Exception as exc:
                logger.warning("ReAct 工具 %s 调用失败：%s", decision.tool, exc)
                tr = ToolResult(
                    tool=decision.tool, query=decision.query, hits=[], quality={},
                    note=f"工具执行异常：{type(exc).__name__}: {str(exc)[:120]}",
                )
            # 合并引用（去重 + 上限截断）；无新命中累计无进展
            added = _merge_citations(result.citations, tr.hits, cfg.max_citations)
            no_progress = 0 if added else no_progress + 1
            elapsed_ms = int((time.monotonic() - round_started) * 1000)

            visible_hits = tr.hits[: cfg.obs_hits]
            yield {
                "type": "observation", "round": round_no, "tool": tr.tool, "query": tr.query,
                "hits": [
                    {
                        "n": i + 1, "chunk_id": c.chunk_id, "document_id": c.document_id,
                        "title": c.title, "doc_no": c.doc_no,
                        "snippet": c.snippet[: cfg.obs_snippet_chars],
                        "full_snippet": c.snippet, "score": round(float(c.score), 4),
                    }
                    for i, c in enumerate(visible_hits)
                ],
                "quality": tr.quality, "note": tr.note, "elapsed_ms": elapsed_ms,
                "hits_total": len(tr.hits),
            }
            history_msgs.append({"role": "assistant", "content": raw})
            history_msgs.append({
                "role": "user",
                "content": format_observation(
                    tool=tr.tool, query=tr.query,
                    hits=[
                        Citation(
                            chunk_id=c.chunk_id, document_id=c.document_id, title=c.title,
                            doc_no=c.doc_no, snippet=c.snippet[: cfg.obs_snippet_chars],
                            score=c.score,
                        )
                        for c in visible_hits
                    ],
                    quality=tr.quality, note=tr.note,
                ),
            })
            result.rounds_log.append({
                "round": round_no, "thought": decision.thought,
                "action": {"tool": decision.tool, "query": decision.query, "skipped": False},
                "observation_hits": len(tr.hits), "observation_note": tr.note,
                "elapsed_ms": elapsed_ms,
            })

        # ---- 无进展收敛：取消轮数上限后，这是防止"无限空转"的主要保障。
        # 语义分层（重要）：
        #   - 达到 limit-1 轮：注入一次"换角度或收尾"提示（给模型纠偏机会）；
        #   - 达到 limit 轮：**强制收尾**（stop_reason=no_progress），进入最终生成。
        # 措辞红线：引导换角度或基于通用知识**完成作答**，不是劝退拒答。
        limit = max(2, cfg.no_progress_limit)
        if no_progress >= limit:
            result.stop_reason = "no_progress"
            logger.info("ReAct 连续 %d 轮无新引用，强制收尾", no_progress)
            break
        if no_progress >= limit - 1:
            history_msgs.append({
                "role": "user",
                "content": "[系统提示] 已连续多轮未获得新材料。请二选一："
                "① 更换关键词/换角度再做最后一次尝试；"
                "② 基于已有材料与通用公文知识 finish 完成作答"
                "（**必须完成任务，不要拒答**）。",
            })


    stop_label = STOP_REASON_LABELS.get(result.stop_reason, result.stop_reason)
    yield {
        "type": "react_end",
        "rounds": result.rounds,
        "stop_reason": result.stop_reason,
        "stop_label": stop_label,
        "citations_total": len(result.citations),
        "total_ms": int((time.monotonic() - started) * 1000),
    }
    result.trace_text = build_trace_text(
        analysis=analysis,
        rounds_log=result.rounds_log,
        stop_reason=result.stop_reason,
        stop_label=stop_label,
        rounds=result.rounds,
        citations_total=len(result.citations),
    )
    logger.info(
        "ReAct 循环结束：rounds=%d stop=%s citations=%d",
        result.rounds, result.stop_reason, len(result.citations),
    )


# 提问理解 system prompt（在 react_prompt 中定义，此处引用避免循环导入的延迟绑定问题）
from app.services.agent.react_prompt import UNDERSTAND_SYSTEM as _UNDERSTAND_SYSTEM  # noqa: E402
