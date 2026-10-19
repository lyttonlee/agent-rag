"""ReAct 引擎单测：协议解析 + 全部护栏 + 引用聚合（用户 2026-10-16 深度推理模块）。

不连真实 DB / 不调真实模型：用 mock 模型（按脚本返回决策 JSON）与
mock 工具（返回固定命中），驱动 run_react 异步生成器，验证：

1. **事件序列**：react_start → analysis → (react_round/thought/action/
   observation)×N → react_end，事件类型与顺序正确（前端契约）；
2. **护栏收敛**（循环不可能无限悬空）：
   - 轮数上限：模型永远不 finish → 到 max_rounds 停（stop=max_rounds）；
   - 预算：budget_ms=0 → 首轮即停（stop=budget）；
   - 解析失败：连续输出非法 JSON 2 次 → stop=parse_fail；
   - 决策失败：模型连续抛异常 2 次 → stop=decision_fail；
   - query 去重：同一 query 二次出现 → 跳过真实工具调用；
   - 无进展：连续空命中 → 注入收敛提示（观察 note 含系统提示语义）；
3. **引用聚合**：跨轮去重、上限截断、新增计数驱动无进展；
4. **trace 文本**：含每轮「思考/行动/观察」与停止原因（落库与回看契约）；
5. **协议解析**：宽容 JSON、字段校验、工具名小写归一；
6. **配置档位**：fast/deep/非法档位解析。
"""

from __future__ import annotations

import json

import pytest

from app.schemas.contracts import Citation, ChatRequest
from app.services.agent import react_engine as engine
from app.services.agent.react_config import ReactConfig
from app.services.agent.react_engine import ReactRunResult, _merge_citations, _normalize_query
from app.services.agent.react_prompt import (
    AnalysisResult,
    build_trace_text,
    parse_analysis,
    parse_decision,
)


# =========================================================================
# 协议解析（纯函数）
# =========================================================================

def test_parse_decision_tolerant():
    """宽容解析：允许 JSON 前后夹杂文字、工具名大小写归一。"""
    d = parse_decision('思考中 {"thought":"需要A","action":{"tool":"Knowledge_Search","query":"A 政策"}} 尾巴')
    assert d is not None
    assert d.thought == "需要A"
    assert d.tool == "knowledge_search"
    assert d.query == "A 政策"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "完全不是 JSON",
        '{"thought": 123, "action": {"tool": "x", "query": "y"}}',   # thought 非字符串
        '{"thought": "t", "action": "knowledge_search"}',            # action 非对象
        '{"thought": "t", "action": {"query": "y"}}',                 # 缺 tool
        '{"thought": "t", "action": {"tool": "x", "query": 5}}',      # query 非字符串
    ],
)
def test_parse_decision_invalid(raw):
    """非法输入一律返回 None（由调用方计数并降级，不抛异常）。"""
    assert parse_decision(raw) is None


def test_parse_decision_finish_and_missing_query():
    """finish 动作与缺省 query（视为空串）均可解析。"""
    d = parse_decision('{"thought":"够了","action":{"tool":"finish"}}')
    assert d is not None and d.tool == "finish" and d.query == ""


def test_parse_analysis():
    """查询分析：多子问题与单跳两种形态。"""
    a = parse_analysis('前言 {"restate":"对比A与B","needs_knowledge":true,"candidates":["A销售","B销售"]} 后记')
    assert a is not None and a.needs_knowledge is True and len(a.candidates) == 2
    b = parse_analysis('{"restate":"格式常识","needs_knowledge":false,"needs_knowledge_reason":"通用知识"}')
    assert b is not None and b.needs_knowledge is False and b.candidates == []
    assert parse_analysis("garbage") is None


def test_react_config_for_mode():
    """档位解析（2026-10-19 语义变更）：档位只影响检索广度，轮数默认不限。

    历史实现按档位给 2 轮/5 轮 + 60s/180s；取消轮数上限后档位改为
    fast=窄召回、deep=宽召回，max_rounds/budget_ms 两档一致（默认 0=不限）。
    """
    cfg = ReactConfig()
    fast, deep = cfg.for_mode("fast"), cfg.for_mode("deep")
    # 轮数与预算不随档位变化（默认都不限）
    assert fast.max_rounds == deep.max_rounds == 0
    assert fast.budget_ms == deep.budget_ms == 0
    # 档位差异体现在检索广度
    assert fast.retrieve_top_n < deep.retrieve_top_n
    assert fast.obs_hits < deep.obs_hits
    # 缺省/非法档位回落默认档
    assert cfg.for_mode(None).retrieve_top_n == cfg.for_mode(cfg.default_mode).retrieve_top_n
    assert cfg.for_mode("不存在的档位").retrieve_top_n == cfg.for_mode(cfg.default_mode).retrieve_top_n
    assert cfg.resolved_mode("不存在的档位") == cfg.default_mode


def test_react_config_defaults_unlimited():
    """红线（2026-10-19）：默认不设轮数/时长上限——0 表示"不限"，不是"零轮"。"""
    cfg = ReactConfig()
    assert cfg.max_rounds == 0 and cfg.budget_ms == 0
    assert cfg.no_progress_limit >= 2, "取消轮数上限后必须有替代的收敛护栏"


def test_normalize_query_strips_punctuation():
    """query 归一化去标点空白并小写（重复检索判定基础）。"""
    assert _normalize_query("A 产品， 华东区!") == _normalize_query("a产品华东区")


def test_merge_citations_dedup_and_cap():
    """引用聚合：按 chunk_id 去重保留先到者，且受上限截断。"""
    existing = [Citation(chunk_id="c1", document_id="d", title="A", snippet="s", score=0.5)]
    added = _merge_citations(
        existing,
        [
            Citation(chunk_id="c1", document_id="d", title="A", snippet="dup", score=0.1),
            Citation(chunk_id="c2", document_id="d", title="B", snippet="s", score=0.4),
        ],
        cap=10,
    )
    assert added == 1 and len(existing) == 2 and existing[0].snippet == "s"
    # 上限截断
    pool = [Citation(chunk_id=f"x{i}", document_id="d", title="t", snippet="s", score=0.1) for i in range(5)]
    target: list[Citation] = []
    _merge_citations(target, pool, cap=3)
    assert len(target) == 3


# =========================================================================
# 主循环（mock 模型 + mock 工具）
# =========================================================================

def _decision(tool: str, query: str = "", thought: str = "思考") -> str:
    return json.dumps({"thought": thought, "action": {"tool": tool, "query": query}}, ensure_ascii=False)


def _hit(i: int) -> Citation:
    return Citation(
        chunk_id=f"c{i}", document_id="d", title=f"《文件{i}》", doc_no=f"A-{i}",
        snippet=f"片段{i}内容", score=0.5 + i / 100,
    )


class _FakeProvider:
    """按脚本返回决策输出的假模型（同时服务查询分析与决策调用）。

    script: 依次弹出的返回值；元素为 str 直接返回，为 Exception 则抛出。
    弹尽后重复最后一个元素（模拟模型固执地不 finish）。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.model_name = "fake-gen"

    async def generate(self, messages, **kwargs):
        self.calls.append(messages)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _install(monkeypatch, script: list, tool_impl=None) -> _FakeProvider:
    """注入假模型与假工具执行器，返回假模型实例（便于断言决策历史）。"""
    provider = _FakeProvider(script)
    monkeypatch.setattr(engine, "ModelProvider", lambda role=None: provider)

    async def fake_tool(db, req, tool, args, cfg, caller=None):
        if tool_impl is not None:
            return await tool_impl(tool, args)
        return engine.ToolResult(tool=tool, query=args.get("query", ""), hits=[_hit(1)])

    monkeypatch.setattr(engine, "execute_tool", fake_tool)
    return provider


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _cfg(max_rounds: int = 0, budget_ms: int = 0, **kw) -> ReactConfig:
    """构造测试用配置。

    注意语义（2026-10-19）：``max_rounds=0`` / ``budget_ms=0`` 表示**不限**，
    不是"立刻结束"。护栏类测试必须显式给正数，否则循环不会因为该护栏退出。
    """
    return ReactConfig(max_rounds=max_rounds, budget_ms=budget_ms, **kw)


@pytest.mark.asyncio
async def test_react_two_round_finish(monkeypatch):
    """标准两轮：search → finish，事件序列与引用聚合正确。"""
    calls: list[int] = []

    async def tool(tool_name, args):
        calls.append(1)
        return engine.ToolResult(
            tool=tool_name, query=args["query"],
            hits=[_hit(len(calls))], quality={"keyword_hits": 2}, note="",
        )

    _install(monkeypatch, [
        json.dumps({"restate": "对比A与B", "needs_knowledge": True, "candidates": ["A销售", "B销售"]}),
        _decision("knowledge_search", "A 产品 华东区 销售额"),
        _decision("finish", thought="材料已覆盖，可作答"),
    ], tool_impl=tool)

    cfg = ReactConfig()
    req = ChatRequest(query="对比 A 和 B 在华东区的销售")
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, req, _cfg(5, 180000), result)]

    # 事件序列：react_start → analysis → 轮1(round/thought/action/observation) → 轮2(round/thought) → react_end
    types = _types(events)
    # 阶段播报插在 react_start 之后、analysis 之前（2026-10-16 新增：
    # 模型每次调用 1-4 秒，此前这段完全静默，页面像卡住了）
    assert types[0] == "react_start" and types[1] == "phase" and types[2] == "analysis"
    assert types[-1] == "react_end"
    assert types.count("react_round") == 2
    assert types.count("thought") == 2
    assert types.count("action") == 1  # 轮2 是 finish，不算工具 action
    assert types.count("observation") == 1
    assert types.index("react_round") < types.index("thought") < types.index("action") < types.index("observation")

    # 结论
    assert result.stop_reason == "final"
    assert result.rounds == 2
    assert len(result.citations) == 1  # 两次工具各返 1 条不同命中
    assert result.analysis.needs_knowledge is True

    # trace 文本含每轮 TAO 与停止原因（落库/回看契约）
    assert "【第1轮】" in result.trace_text and "knowledge_search" in result.trace_text
    assert "决定：直接作答" in result.trace_text and "信息充分" in result.trace_text


@pytest.mark.asyncio
async def test_react_max_rounds_guard(monkeypatch):
    """护栏：模型永不 finish 时按 max_rounds 收敛（不悬空）。"""

    async def tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[])

    _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("knowledge_search", "同一个 query"),
    ], tool_impl=tool)

    cfg = ReactConfig()
    result = ReactRunResult()
    # no_progress_limit 抬高，隔离本测试关注的"轮数硬闸"
    events = [e async for e in engine.run_react(
        None, ChatRequest(query="q"), _cfg(3, 180000, no_progress_limit=50), result
    )]

    assert result.stop_reason == "max_rounds"
    assert result.rounds == 3
    assert _types(events).count("react_round") == 3
    # 去重：只有第一次真实调用了工具，其余 skipped
    actions = [e for e in events if e["type"] == "action"]
    assert sum(1 for a in actions if a["skipped"]) == 2


@pytest.mark.asyncio
async def test_react_budget_guard(monkeypatch):
    """护栏：**显式设置**预算且已超时 → 首轮前即收敛（stop=budget）。

    注意：``budget_ms=0`` 现在的语义是"不限时长"，因此本测试改用极小预算 +
    假时钟（每次取样 +10s），避免依赖真实耗时导致抖动。
    """
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(engine.time, "monotonic", fake_monotonic)
    _install(monkeypatch, [json.dumps({"restate": "单一事实查询", "needs_knowledge": True})])
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 500), result)]
    assert result.stop_reason == "budget"
    assert _types(events).count("react_round") == 0  # 未进入任何轮次
    assert result.citations == []


@pytest.mark.asyncio
async def test_react_budget_zero_means_unlimited(monkeypatch):
    """红线：budget_ms=0 表示不限时长（不能像旧实现那样"首轮即停"）。"""

    async def tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[_hit(1)])

    _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("knowledge_search", "q1"),
        _decision("finish", thought="够了"),
    ], tool_impl=tool)
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(0, 0), result)]
    assert result.stop_reason == "final"
    assert _types(events).count("react_round") == 2


@pytest.mark.asyncio
async def test_react_parse_fail_guard(monkeypatch):
    """护栏：连续两次非法 JSON → parse_fail 退出（第一次回灌纠错继续）。"""
    _install(monkeypatch, [json.dumps({"restate": "单一事实查询", "needs_knowledge": True}), "非 JSON 输出 A", "非 JSON 输出 B"])
    cfg = ReactConfig()
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 180000), result)]
    assert result.stop_reason == "parse_fail"
    assert result.rounds == 2  # 失败两轮后退出
    # 第二次失败前执行过一次纠错回灌（provider 被调用 3 次：分析 + 2 轮决策）
    assert "决策失败" in result.trace_text


@pytest.mark.asyncio
async def test_react_decision_fail_guard(monkeypatch):
    """护栏：决策调用连续异常 → decision_fail 退出，零引用（策略层兜底）。"""
    _install(monkeypatch, [json.dumps({"restate": "单一事实查询", "needs_knowledge": True}), RuntimeError("boom"), RuntimeError("boom")])
    cfg = ReactConfig()
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 180000), result)]
    assert result.stop_reason == "decision_fail"
    assert result.citations == []


@pytest.mark.asyncio
async def test_react_unknown_tool_and_missing_query(monkeypatch):
    """护栏：未知工具与缺 query 均不崩，以观察提示回灌模型。"""
    async def tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[_hit(9)])

    _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("web_search", "随便"),                 # 未知工具
        _decision("knowledge_search", ""),                 # 空 query
        _decision("finish"),
    ], tool_impl=tool)

    cfg = ReactConfig()
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 180000), result)]
    notes = [e.get("note", "") for e in events if e["type"] == "observation"]
    assert any("未知工具" in n for n in notes)
    assert any("缺少 query" in n for n in notes)
    assert result.stop_reason == "final"


@pytest.mark.asyncio
async def test_react_tool_exception_is_note(monkeypatch):
    """工具执行抛异常 → 转为观察 note，不中断循环（可继续 finish）。"""
    async def boom(tool_name, args):
        raise RuntimeError("工具内部炸了")

    _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("knowledge_search", "q1"),
        _decision("finish"),
    ], tool_impl=boom)

    cfg = ReactConfig()
    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 180000), result)]
    notes = [e.get("note", "") for e in events if e["type"] == "observation"]
    assert any("工具执行异常" in n for n in notes)
    assert result.stop_reason == "final"


@pytest.mark.asyncio
async def test_react_first_decision_sees_user_query(monkeypatch):
    """红线：决策调用必须拿到原始问题（否则模型会"未收到问题"直接 finish）。

    真实端到端踩坑：决策历史仅含 system prompt 时，模型回
    「用户尚未提出具体问题」→ 0 引用 finish。本测试锁死问题注入。
    """
    provider = _install(monkeypatch, [
        json.dumps({"restate": "简单问题", "needs_knowledge": False, "needs_knowledge_reason": "通用常识"}),
        _decision("finish"),
    ])
    cfg = ReactConfig()
    result = ReactRunResult()
    [e async for e in engine.run_react(None, ChatRequest(query="安全生产通知的审批流程是什么"), _cfg(2, 60000), result)]

    # 第一次决策调用（calls[1]）的历史中必须含用户原始问题
    first_decision_history = provider.calls[1]
    joined = " ".join(m["content"] for m in first_decision_history)
    assert "安全生产通知的审批流程是什么" in joined, "决策调用未拿到用户问题（致命）"


@pytest.mark.asyncio
async def test_react_no_progress_converges(monkeypatch):
    """护栏：连续无新命中时先注入收敛提示（给模型纠偏机会）。"""

    async def empty_tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[], note="未检索到相关内容")

    provider = _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("knowledge_search", "q1"),
        _decision("knowledge_search", "q2"),
        _decision("finish"),
    ], tool_impl=empty_tool)

    cfg = ReactConfig()
    result = ReactRunResult()
    [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(5, 180000), result)]

    # 第 3 次决策调用（即第 2 轮之后）的历史里应出现收敛提示
    last_decision_history = provider.calls[-1]
    joined = " ".join(m["content"] for m in last_decision_history)
    assert "连续多轮未获得新材料" in joined and "不要拒答" in joined


@pytest.mark.asyncio
async def test_react_no_progress_hard_stop(monkeypatch):
    """护栏（2026-10-19 新增）：取消轮数上限后，连续 N 轮无新材料必须强制收尾。

    这是"不限轮 ≠ 无限空转"的保险丝：模型持续换关键词但一无所获时，
    达到 no_progress_limit 即停（stop=no_progress），随后进入最终生成
    （仍要完成任务、不拒答，由策略层保证）。
    """

    async def empty_tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[], note="未检索到相关内容")

    # 模型永远换关键词检索、永不 finish；无进展护栏必须兜住
    _install(monkeypatch, [
        json.dumps({"restate": "单一事实查询", "needs_knowledge": True}),
        _decision("knowledge_search", "q1"),
        _decision("knowledge_search", "q2"),
        _decision("knowledge_search", "q3"),
        _decision("knowledge_search", "q4"),
        _decision("knowledge_search", "q5"),
    ], tool_impl=empty_tool)

    result = ReactRunResult()
    events = [e async for e in engine.run_react(
        None, ChatRequest(query="q"), _cfg(0, 0, no_progress_limit=3), result
    )]
    assert result.stop_reason == "no_progress"
    assert result.rounds == 3, "第 3 轮达到无进展上限即收尾"
    end = next(e for e in events if e["type"] == "react_end")
    assert end["stop_reason"] == "no_progress" and "强制收尾" in end["stop_label"]
    assert "无新材料" in result.trace_text


@pytest.mark.asyncio
async def test_react_unlimited_rounds_runs_until_finish(monkeypatch):
    """红线（2026-10-19）：默认不限轮数——模型需要多少轮就做多少轮。

    旧实现 fast=2/deep=5 会在第 2/5 轮硬截断；现在只有模型自己 finish
    （或护栏触发）才停。本测试让模型连续检索 6 轮后 finish。
    """
    counter = {"n": 0}

    async def tool(tool_name, args):
        counter["n"] += 1
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[_hit(counter["n"])])

    script = [json.dumps({"restate": "多步排查", "needs_knowledge": True})]
    script += [_decision("knowledge_search", f"q{i}") for i in range(1, 7)]
    script += [_decision("finish", thought="材料已足够")]
    _install(monkeypatch, script, tool_impl=tool)

    result = ReactRunResult()
    events = [e async for e in engine.run_react(None, ChatRequest(query="q"), _cfg(), result)]
    assert result.stop_reason == "final"
    assert result.rounds == 7, "第 7 轮才 finish，不限轮数时不能被提前截断"
    assert _types(events).count("react_round") == 7
    # react_start 必须显式告知客户端"不限轮数"
    start = next(e for e in events if e["type"] == "react_start")
    assert start["max_rounds"] == 0 and start["unlimited_rounds"] is True


def test_build_trace_text_handles_failed_round():
    """trace 文本：决策失败轮（action=None）渲染为失败痕迹，不报错。"""
    txt = build_trace_text(
        analysis=AnalysisResult(restate="简单", needs_knowledge=False),
        rounds_log=[
            {"round": 1, "thought": "", "action": None, "observation_hits": 0, "observation_note": "决策调用失败"},
            {"round": 2, "thought": "够了", "action": {"tool": "finish", "query": ""}},
        ],
        stop_reason="final", stop_label="信息充分", rounds=2, citations_total=0,
    )
    assert "【第1轮】决策失败" in txt
    assert "【第2轮】" in txt and "决定：直接作答" in txt

# =========================================================================
# 用户 2026-10-16 追加要求：工具自主权 / 不降级 / 多轮上下文
# =========================================================================

def test_build_history_hint_injects_multiturn_context():
    """多轮上下文：会话历史被转成决策提示（指代消解的前提）。"""
    from app.services.agent.react_engine import build_history_hint

    text = build_history_hint([("user", "安全生产有哪些要求"), ("assistant", "主要有三条")])
    assert "本次会话历史" in text
    assert "用户：安全生产有哪些要求" in text
    assert "我：主要有三条" in text
    # 空历史返回空串（不注入噪声）
    assert build_history_hint([]) == ""
    assert build_history_hint(None) == ""


@pytest.mark.asyncio
async def test_react_history_injected_into_decision_prompt(monkeypatch):
    """红线：多轮追问时，历史必须出现在决策上下文里（否则模型不知道"它"指谁）。"""
    async def tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[])

    provider = _install(monkeypatch, [
        json.dumps({"restate": "追问细节", "needs_knowledge": True}),
        _decision("finish", thought="结合上文回答"),
    ], tool_impl=tool)
    cfg = ReactConfig()
    result = ReactRunResult()
    history = [("user", "安全生产有哪些要求"), ("assistant", "有三条规定")]

    async for _ in engine.run_react(
        None, ChatRequest(query="第二条的具体要求是什么"), _cfg(3, 60000), result, history=history
    ):
        pass

    first_user = provider.calls[1][1]["content"]
    assert "本次会话历史" in first_user
    assert "安全生产有哪些要求" in first_user
    assert "第二条的具体要求是什么" in first_user


@pytest.mark.asyncio
async def test_react_model_can_finish_without_any_tool(monkeypatch):
    """红线：模型自主决定不查库 → 第 1 轮直接 finish，零工具调用。"""
    tool_calls = []

    async def tool(tool_name, args):
        tool_calls.append(tool_name)
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[])

    provider = _install(monkeypatch, [
        json.dumps({"restate": "公文格式常识", "needs_knowledge": False,
                    "needs_knowledge_reason": "属通用知识"}),
        _decision("finish", thought="通用格式常识，无需检索"),
    ], tool_impl=tool)
    cfg = ReactConfig()
    result = ReactRunResult()

    events = [e async for e in engine.run_react(
        None, ChatRequest(query="公文标题怎么写"), _cfg(5, 60000), result
    )]

    assert tool_calls == [], "模型未要求检索时不应调用任何工具"
    assert result.stop_reason == "final"
    assert result.citations == []
    types = [e["type"] for e in events]
    assert "action" not in types and "observation" not in types, "无工具调用就不该有 action/observation"
    # 决策 system 必须明确告知"自主决定是否检索"
    system = provider.calls[1][0]["content"]
    assert "由你根据问题自行判断" in system


@pytest.mark.asyncio
async def test_react_model_can_read_document(monkeypatch):
    """红线：模型可自主调用 read_document 打开原文核对（不限于检索）。"""
    seen_tools = []

    async def tool(tool_name, args):
        seen_tools.append(tool_name)
        return engine.ToolResult(
            tool=tool_name, query=args["query"],
            hits=[_hit(9)], quality={"chunk_count": 4}, note="已打开原文",
        )

    _install(monkeypatch, [
        json.dumps({"restate": "需看清具体条款", "needs_knowledge": True}),
        _decision("read_document", query="关于安全生产的通知", thought="片段不够，看原文"),
        _decision("finish", thought="原文已看清"),
    ], tool_impl=tool)
    cfg = ReactConfig()
    result = ReactRunResult()

    async for _ in engine.run_react(
        None, ChatRequest(query="安全生产通知具体怎么规定的"), _cfg(3, 60000), result
    ):
        pass

    assert seen_tools == ["read_document"], "模型点名的工具必须真的被执行"
    assert len(result.citations) == 1


@pytest.mark.asyncio
async def test_react_understand_failure_does_not_block_loop(monkeypatch):
    """红线：**不降级**——理解阶段失败只是少一段参考，循环照常进行。"""
    async def tool(tool_name, args):
        return engine.ToolResult(tool=tool_name, query=args["query"], hits=[_hit(1)])

    _install(monkeypatch, [
        RuntimeError("理解阶段挂了"),      # 理解阶段直接抛异常
        _decision("knowledge_search", query="安全生产", thought="检索"),
        _decision("finish", thought="够了"),
    ], tool_impl=tool)
    cfg = ReactConfig()
    result = ReactRunResult()

    events = [e async for e in engine.run_react(
        None, ChatRequest(query="安全生产规定"), _cfg(3, 60000), result
    )]

    types = [e["type"] for e in events]
    assert types.count("react_round") == 2, "理解失败后循环必须照常跑满"
    assert result.stop_reason == "final"
    assert len(result.citations) == 1
    # analysis 事件仍要发（前端要显示"理解阶段跳过"）
    analysis_evt = next(e for e in events if e["type"] == "analysis")
    assert "跳过" in analysis_evt["restate"]


def test_analysis_never_gates_the_loop():
    """红线：AnalysisResult 无论 needs_knowledge 取值都不含任何路由字段。

    旧实现有 single_hop 并据此"单跳直接检索"短路；现已删除该字段——
    结构上保证引擎无法基于理解阶段做分支决策。
    """
    a = AnalysisResult(restate="x", needs_knowledge=True)
    b = AnalysisResult(restate="x", needs_knowledge=False)
    assert not hasattr(a, "single_hop")
    assert not hasattr(b, "single_hop")
    # 两者的提示文本都必须声明"动作由模型自己决定"
    for item in (a, b):
        assert "下一步动作由你在本轮自行决定" in item.as_hint_text()


def test_export_prompts_covers_all_stages():
    """提示词管理面板数据源：四个阶段齐全、含工具清单、区分来源与可编辑性。"""
    from app.services.agent.react_prompt import export_prompts

    items = export_prompts("- knowledge_search：检索", 0)
    keys = [i["key"] for i in items]
    assert keys == [
        "deep_prompt_understand", "deep_prompt_decision",
        "deep_prompt_final", "deep_prompt_observation",
    ]
    decision = next(i for i in items if i["key"] == "deep_prompt_decision")
    assert "knowledge_search：检索" in decision["content"]
    # 默认不限轮数：提示词里应写"不设轮数上限"而不是"5 轮"
    assert "不设轮数上限" in decision["content"]
    assert "5 轮" not in decision["content"]
    # 每项都带默认值 + 来源 + 可编辑标记（前端"恢复默认"依赖它）
    assert all(i["content"].strip() and i["default_content"].strip() for i in items)
    assert decision["source"] == "default" and decision["editable"] is True
    obs = next(i for i in items if i["key"] == "deep_prompt_observation")
    assert obs["editable"] is False and obs["source"] == "builtin"


def test_export_prompts_reflects_custom_override():
    """配置覆盖后：面板返回覆盖文本并标注 source=custom（所见即所跑）。"""
    from app.services.agent.react_prompt import export_prompts

    items = export_prompts(
        "- knowledge_search：检索",
        0,
        {
            "deep_prompt_understand": "自定义理解提示",
            "deep_prompt_decision": "自定义决策：{tool_lines}",
            "deep_prompt_final": "",
        },
    )
    by_key = {i["key"]: i for i in items}
    assert by_key["deep_prompt_understand"]["content"] == "自定义理解提示"
    assert by_key["deep_prompt_understand"]["source"] == "custom"
    # 决策覆盖里的 {tool_lines} 必须被注入（否则模型不知道有哪些工具）
    assert "- knowledge_search：检索" in by_key["deep_prompt_decision"]["content"]
    assert by_key["deep_prompt_decision"]["source"] == "custom"
    # 未覆盖的仍为默认
    assert by_key["deep_prompt_final"]["source"] == "default"


def test_decision_prompt_injects_round_rule_for_limited_and_unlimited():
    """决策提示词的收尾规则：不限轮数 vs 设了硬上限，措辞必须不同。"""
    from app.services.agent.react_prompt import build_decision_system

    unlimited = build_decision_system(0, "- tool", "")
    assert "不设轮数上限" in unlimited and "{round_rule}" not in unlimited
    limited = build_decision_system(5, "- tool", "")
    assert "本轮上限 5 轮" in limited


def test_final_prompt_override_and_material_rule():
    """最终生成提示词可覆盖；{material_rule} 按有无材料注入不同规则。"""
    from app.services.agent.react_prompt import build_final_messages

    no_material = build_final_messages("问题", [], [], [], system_override="规则：{material_rule}")
    sys_no = no_material[0]["content"]
    assert "不得拒答" in sys_no and "规则：" in sys_no

    has_material = build_final_messages(
        "问题", [], [Citation(chunk_id="c", document_id="d", title="T", snippet="s", score=1.0)],
        [], system_override="规则：{material_rule}",
    )
    sys_yes = has_material[0]["content"]
    assert "必须标注引用" in sys_yes and "不得拒答" not in sys_yes
