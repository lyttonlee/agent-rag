"""公文起草多轮上下文——端到端编排集成测试（用户 2026-10-16 致命缺陷）。

不连真实 DB / 不调真实模型：用内存会话存储 + mock 模型，验证
stream_generate 在同一会话内第二轮起草时，确实把"上一稿"注入模型
上下文（此前丢失上一稿、重新生成一份完整文稿的根因）。

覆盖：
- 首轮：messages 不含任何往轮草稿（正确行为）；
- 第二轮（同一 conversation_id）：messages 必须包含第一轮的 assistant
  草稿正文——否则"在上一稿基础上修改"无从谈起；
- 两条链路都验证：**深度推理起草**（deep_reason，默认开，2026-10-19）
  与**单趟旧链路**（deep_reason=false）——多轮注入是两条链路共用的契约。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.schemas.contracts import Citation, GenerateRequest
from app.services import generation
from app.services.agent.react_config import ReactConfig
from app.services.agent.react_prompt import DRAFT_PACK


class _Msg:
    """recent_history 返回的最小消息桩（带 role / content）。"""

    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _Conv:
    id = "conv-A"


def _parse_sse(chunks: list[str]) -> list[dict]:
    """把 SSE 文本片段解析为事件字典列表。"""
    events: list[dict] = []
    for c in chunks:
        line = c.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def _install_common(monkeypatch, store: dict):
    """注入会话存储桩（不参与断言，仅让链路跑通）。"""
    async def fake_create(db, conv_type, doc_type=None, space_ids=None):
        return _Conv()

    async def fake_append(db, conversation_id, role, content, **kw):
        store.setdefault(conversation_id, []).append(_Msg(role, content))

    async def fake_recent(db, conversation_id, limit=10):
        return list(store.get(conversation_id, []))

    monkeypatch.setattr(generation.conv_service, "create_conversation", fake_create)
    monkeypatch.setattr(generation.conv_service, "append_message", fake_append)
    monkeypatch.setattr(generation.conv_service, "recent_history", fake_recent)

    async def _always_conv_a(db, req, doc_type=None):
        return "conv-A"
    monkeypatch.setattr(generation, "_ensure_draft_conversation", _always_conv_a)

    async def fake_sensitive(db, req):
        return []
    monkeypatch.setattr(generation, "_resolve_sensitive_words", fake_sensitive)

    async def fake_agent(db):
        return SimpleNamespace(enabled=False)
    monkeypatch.setattr(generation, "load_agent_config", fake_agent)


@pytest.mark.asyncio
async def test_stream_generate_injects_prior_draft_single_hop(monkeypatch):
    """单趟链路（deep_reason=false）：第二轮必须把上一稿喂给模型。"""
    store: dict[str, list] = {}
    captured: list = []
    _install_common(monkeypatch, store)

    async def fake_retrieve(db, req):
        return SimpleNamespace(citations=[], truncated_by_security=False)

    monkeypatch.setattr(generation, "retrieve_enhanced", fake_retrieve)

    async def fake_cfg(db):
        return SimpleNamespace(vec_distance_threshold=0.6)
    monkeypatch.setattr(generation, "load_retrieval_config", fake_cfg)

    async def fake_tpl(db):
        return {}
    monkeypatch.setattr(generation, "load_prompt_templates", fake_tpl)

    async def fake_react_cfg(db):
        return ReactConfig(draft_enabled=False)  # 显式关深度推理 → 单趟旧链路
    monkeypatch.setattr(generation, "load_react_config", fake_react_cfg)

    class FakeModelProvider:
        def __init__(self, role=None):
            self.model_name = "fake-gen-model"

        async def generate_stream(self, messages):
            captured.append(messages)
            yield ("content", "第一稿全文：关于开展安全生产大检查的通知\n各有关部门：")

    monkeypatch.setattr(generation, "ModelProvider", FakeModelProvider)

    # ---- 第一轮：无历史，正常起草 ----
    req1 = GenerateRequest(query="起草一份安全生产大检查的通知", deep_reason=False)
    async for _ in generation.stream_generate(db=None, req=req1, actor="t"):
        pass

    # ---- 第二轮：同一会话，提出修改 ----
    req2 = GenerateRequest(query="把标题改短一点", conversation_id="conv-A", deep_reason=False)
    async for _ in generation.stream_generate(db=None, req=req2, actor="t"):
        pass

    assert len(captured) == 2, f"应捕获两轮消息，实际 {len(captured)}"
    # 首轮不应含任何往轮草稿
    assert all("第一稿全文" not in m["content"] for m in captured[0])
    # 第二轮必须把第一稿注入模型上下文（致命缺陷修复的核心断言）
    assert any(
        "第一稿全文" in m["content"] for m in captured[1]
    ), "致命缺陷未修复：第二轮未把上一稿注入模型上下文"


@pytest.mark.asyncio
async def test_stream_generate_react_injects_prior_draft(monkeypatch):
    """深度推理起草链路：第二轮同样必须把上一稿注入成稿上下文。

    同时验证 ReAct 编排契约：
    - react_* 事件原样透传（前端推理面板的数据源）；
    - 循环结论（引用/轮数/过程摘要）进入成稿 messages（【推理过程】+ 引用 [n]）；
    - TAO 全链文本随助手消息 reasoning 落库（历史可回看）；
    - 不拒答：零引用也照常成稿并附 AI 声明（disclaimer=True）。
    """
    store: dict[str, list] = {}
    captured: list = []          # 成稿 messages（每轮一次）
    react_calls: list[dict] = []  # run_react 调用参数
    saved_kwargs: list[dict] = []  # assistant 落库 kwargs
    _install_common(monkeypatch, store)

    async def fake_tpl(db):
        return {}
    monkeypatch.setattr(generation, "load_prompt_templates", fake_tpl)

    async def fake_react_cfg(db):
        return ReactConfig(draft_enabled=True)  # 默认开 → 深度推理起草
    monkeypatch.setattr(generation, "load_react_config", fake_react_cfg)

    # ---- 假 ReAct 引擎：跑脚本化事件序列，结论写入 result ----
    async def fake_run_react(db, req, cfg, result, caller=None, history=None, *, pack=None, task_context=""):
        react_calls.append({"pack": pack, "history": history,
                            "task_context": task_context, "query": req.query})
        assert pack is DRAFT_PACK, "起草链路必须用起草通道提示词包"
        yield {"type": "react_start", "mode": "react", "channel": "draft",
               "max_rounds": 0, "unlimited_rounds": True, "total_budget_ms": 0,
               "tools": ["knowledge_search"]}
        yield {"type": "phase", "phase": "understand", "label": "正在理解你的问题…"}
        yield {"type": "analysis", "restate": "起草安全生产大检查通知",
               "needs_knowledge": True, "needs_knowledge_reason": "",
               "candidates": [], "ambiguities": []}
        yield {"type": "react_round", "round": 1, "max_rounds": 0, "unlimited_rounds": True}
        yield {"type": "thought", "round": 1, "text": "先查库内检查范围规定"}
        yield {"type": "action", "round": 1, "tool": "knowledge_search",
               "query": "安全生产大检查 检查范围", "skipped": False}
        yield {"type": "observation", "round": 1, "tool": "knowledge_search",
               "query": "安全生产大检查 检查范围", "hits": [], "quality": {},
               "note": "命中 3 条", "elapsed_ms": 900, "hits_total": 3}
        yield {"type": "react_end", "rounds": 1, "stop_reason": "final",
               "stop_label": "依据已足够（模型主动结束，开始起草）",
               "citations_total": 1, "total_ms": 4200}
        # 结论写入 result（编排层随后读取）
        result.citations.append(Citation(
            chunk_id="c1", document_id="d1", title="安全生产大检查通知",
            doc_no="川安监〔2026〕12号", snippet="检查范围包括……", score=0.9,
        ))
        result.rounds = 1
        result.stop_reason = "final"
        result.trace_text = "【第1轮】思考：先查库内规定 → 调用：knowledge_search"

    monkeypatch.setattr(generation, "run_react", fake_run_react)

    # 捕获 assistant 落库 kwargs（含 reasoning 全链文本）
    async def fake_append_capture(db, conversation_id, role, content, **kw):
        store.setdefault(conversation_id, []).append(_Msg(role, content))
        if role == "assistant":
            saved_kwargs.append(kw)

    monkeypatch.setattr(generation.conv_service, "append_message", fake_append_capture)

    class FakeModelProvider:
        def __init__(self, role=None):
            self.model_name = "fake-gen-model"

        async def generate_stream(self, messages, max_tokens=4096):
            captured.append(messages)
            yield ("content", "第一稿全文：关于开展安全生产大检查的通知\n各有关部门：")

    monkeypatch.setattr(generation, "ModelProvider", FakeModelProvider)

    # ---- 第一轮（无历史）----
    req1 = GenerateRequest(query="起草一份安全生产大检查的通知")
    events1 = [e async for e in generation.stream_generate(db=None, req=req1, actor="t")]
    parsed1 = _parse_sse(events1)
    types1 = [e["type"] for e in parsed1]
    assert "react_start" in types1 and "react_end" in types1, "深度起草必须透传 ReAct 事件"
    assert "phase" in types1, "阶段播报事件必须透传（修 SSE 静默中断）"
    start = next(e for e in parsed1 if e["type"] == "react_start")
    assert start["channel"] == "draft" and start["unlimited_rounds"] is True
    # 首轮无历史：成稿 messages 不含往轮草稿，也不带【推理过程】之外的杂质
    assert all("第一稿全文" not in m["content"] for m in captured[0])
    assert any("【推理过程】" in m["content"] for m in captured[0]), "成稿 prompt 应带推理过程摘要"
    # 落库 reasoning 含 TAO 全链文本
    assert "【第1轮】" in (saved_kwargs[0].get("reasoning") or ""), \
        "TAO 全链文本必须随助手消息落库（历史可回看）"

    # ---- 第二轮：同一会话提修改（多轮上下文注入）----
    req2 = GenerateRequest(query="把标题改短一点", conversation_id="conv-A")
    events2 = [e async for e in generation.stream_generate(db=None, req=req2, actor="t")]
    parsed2 = _parse_sse(events2)

    last = captured[-1]
    joined = "\n".join(m["content"] for m in last)
    assert "第一稿全文" in joined, "深度起草第二轮也必须注入上一稿"
    assert "【推理过程】" in joined, "成稿 prompt 应带推理过程摘要"
    assert "[1] 《安全生产大检查通知》" in joined, "成稿 prompt 应带引用材料"
    # 决策调用也必须拿到会话历史（指代消解"它/上一稿"）
    assert any(
        c["history"] and any("第一稿全文" in h[1] for h in c["history"])
        for c in react_calls
    ), "ReAct 决策上下文必须注入会话历史"
    # 起草任务上下文（文种/任务类型）必须注入决策提示
    assert any("【任务类型】公文起草" in (c["task_context"] or "") for c in react_calls), \
        "起草通道必须注入任务上下文（文种/任务说明）"
    # done 事件：不拒答 + AI 声明
    done = next(e for e in parsed2 if e["type"] == "done")
    assert done["disclaimer"] is True and done["no_result"] is False
    assert done["answer"].endswith("引用内容请核对原文。"), "深度起草必须附 AI 生成声明"
