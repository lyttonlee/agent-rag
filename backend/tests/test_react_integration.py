"""深度推理（ReAct）策略层集成测试：stream_chat 全链路（用户 2026-10-16）。

不连真实 DB / 不调真实模型：mock 会话存储、检索、模型与配置，驱动
stream_chat 消费完整 SSE 序列，验证策略层的**编排契约**：

1. **默认全开接管**：deep_reason 未显式给（配置默认开）时，问答类意图
   （direct_qa/complex_task）被改写为 deep_reason 并进入 TAO 循环；
2. **显式关闭回退**：deep_reason=false 时不产生任何 react_* 事件
   （完全回到旧直答链路）；
3. **闲聊/拒答不受影响**：chitchat/out_of_scope 不进入 ReAct；
4. **SSE 事件序列**：react_start → analysis → react_round/thought/action/
   observation → react_end → meta → token → done；
5. **落库**：TAO 全链文本随 assistant 消息 reasoning 落库；
6. **无材料红线**：循环零引用 + 复检仍无 → 委婉拒答（不调最终生成、
   no_result=true）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.schemas.contracts import ChatRequest, Citation
from app.services import chat as chat_service


class _Msg:
    def __init__(self, role: str, content: str):
        self.role, self.content = role, content


class _Conv:
    id = "conv-react"
    space_ids: list[str] = []


def _parse_sse(chunks: list[str]) -> list[dict]:
    """把 SSE 文本片段解析为事件字典列表。"""
    events: list[dict] = []
    for c in chunks:
        line = c.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def _install_base(monkeypatch, *, agent_enabled: bool = True, react_enabled: bool = True):
    """注入会话/检索/配置层桩，返回 saved 记录容器（供落库断言）。"""
    saved: list[dict] = []
    store: dict[str, list[_Msg]] = {}

    async def fake_create(db, conv_type, doc_type=None, space_ids=None):
        return _Conv()

    async def fake_append(db, conversation_id, role, content, **kw):
        store.setdefault(conversation_id, []).append(_Msg(role, content))
        if role == "assistant":
            saved.append(kw)

    async def fake_recent(db, conversation_id, limit=10):
        return list(store.get(conversation_id, []))

    monkeypatch.setattr(chat_service.conv_service, "create_conversation", fake_create)
    monkeypatch.setattr(chat_service.conv_service, "append_message", fake_append)
    monkeypatch.setattr(chat_service.conv_service, "recent_history", fake_recent)

    async def fake_audit(*a, **kw):
        return None

    monkeypatch.setattr(chat_service, "record_audit", fake_audit)

    async def fake_agent_cfg(db):
        return SimpleNamespace(
            enabled=agent_enabled, llm_classify=False,
            llm_classify_timeout_ms=3000, max_steps=6,
        )

    async def fake_react_cfg(db):
        # 用真实 ReactConfig（保留 for_mode 档位逻辑），仅改轮数/预算加速测试
        from dataclasses import replace

        from app.services.agent.react_config import ReactConfig

        return replace(
            ReactConfig(), enabled=react_enabled,
            fast_max_rounds=5, deep_max_rounds=5,
        )

    monkeypatch.setattr(chat_service, "load_agent_config", fake_agent_cfg)
    monkeypatch.setattr(chat_service, "load_react_config", fake_react_cfg)

    async def fake_sensitive(db, req):
        return []

    monkeypatch.setattr(chat_service, "_load_sensitive_words", fake_sensitive)
    return saved


def _install_models(monkeypatch, script: list, tool_hits: list[Citation] | None = None,
                    final_answer: str = "综合多轮检索后的答案。"):
    """注入假模型（分析/决策/最终生成）与假工具；返回决策历史容器。"""
    decisions: list[list[dict]] = []

    class FakeGen:
        """role=gen 的假 Provider：按脚本返回决策；最终生成流式输出固定答案。"""

        def __init__(self, role=None):
            self.role = role
            self.model_name = "fake-gen"

        async def generate(self, messages, **kwargs):
            decisions.append(messages)
            item = script[min(len(decisions) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        async def generate_stream(self, messages, **kwargs):
            yield ("content", final_answer)

    # 两处 ModelProvider 都要 patch：编排层（chat.py，最终生成）与
    # 引擎层（react_engine.py，查询分析 + 每轮决策）——漏掉任一处都会
    # 打到真实模型。
    from app.services.agent import react_engine as engine

    monkeypatch.setattr(chat_service, "ModelProvider", lambda role=None: FakeGen(role))
    monkeypatch.setattr(engine, "ModelProvider", lambda role=None: FakeGen(role))

    # 引擎层的工具：mock 到 react_engine 的 execute_tool
    async def fake_tool(db, req, tool, args, cfg, caller=None):
        hits = tool_hits if tool_hits is not None else []
        return engine.ToolResult(tool=tool, query=args.get("query", ""), hits=hits, quality={})

    monkeypatch.setattr(engine, "execute_tool", fake_tool)
    return decisions


@pytest.mark.asyncio
async def test_stream_chat_react_default_on_and_events(monkeypatch):
    """默认全开：问答类意图被接管，SSE 全序列完整，TAO 全链落库。"""
    saved = _install_base(monkeypatch)
    _install_models(
        monkeypatch,
        script=[
            json.dumps({"restate": "对比A与B", "needs_knowledge": True, "candidates": ["A销售", "B销售"]}),
            json.dumps({"thought": "先查 A", "action": {"tool": "knowledge_search", "query": "A 销售"}}),
            json.dumps({"thought": "够了", "action": {"tool": "finish", "query": ""}}),
        ],
        tool_hits=[Citation(chunk_id="c1", document_id="d1", title="《A文件》", doc_no="A-1",
                            snippet="A 销售额 100 万", score=0.9)],
    )

    chunks = [c async for c in chat_service.stream_chat(None, ChatRequest(query="对比 A 和 B 的销售"))]
    events = _parse_sse(chunks)
    types = [e["type"] for e in events]

    # TAO 事件序列（编排层先发 phase「正在准备」，再进 ReAct）
    assert "phase" in types
    assert "react_start" in types
    assert types.index("phase") < types.index("react_start") < types.index("analysis")
    # 阶段播报覆盖全流程（用户反馈"页面像断了"的修复点）
    phases = [e["phase"] for e in events if e["type"] == "phase"]
    assert "understand" in phases, "理解阶段应有播报"
    assert "decide" in phases, "每轮决策应有播报"
    assert "tool" in phases, "工具执行应有播报"
    assert "answer" in phases, "最终生成应有播报"
    assert "react_round" in types and "thought" in types and "observation" in types
    assert types.index("react_round") < types.index("thought") < types.index("observation")
    assert "react_end" in types
    # 最终答案仍走既有 meta/token/done
    assert types[-1] == "done"
    assert "meta" in types and "token" in types
    # 意图为 deep_reason（meta 如实回报）
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["intent"] == "deep_reason"
    assert meta["citations"]

    # 落库：TAO 全链文本写入 reasoning
    assert saved, "助手消息未落库"
    assert "【第1轮】" in saved[0]["reasoning"]
    assert "knowledge_search" in saved[0]["reasoning"]
    assert saved[0]["intent"] == "deep_reason"


@pytest.mark.asyncio
async def test_stream_chat_react_disabled_by_request(monkeypatch):
    """显式 deep_reason=false：完全回退普通直答，不产生任何 react_* 事件。"""
    _install_base(monkeypatch)

    async def fake_retrieve(db, req):
        return SimpleNamespace(
            citations=[Citation(chunk_id="c1", document_id="d", title="《旧链路》", snippet="s", score=0.8)],
            truncated_by_security=False, top_vector_distance=0.3, keyword_hits=2, trace=None,
        )

    monkeypatch.setattr(chat_service, "retrieve_enhanced", fake_retrieve)

    async def fake_retrieval_cfg(db):
        return SimpleNamespace(vec_distance_threshold=0.6)

    monkeypatch.setattr(chat_service, "load_retrieval_config", fake_retrieval_cfg)

    async def fake_prompt_tpl(db):
        return {}

    monkeypatch.setattr(chat_service, "load_prompt_templates", fake_prompt_tpl)
    _install_models(monkeypatch, script=["unused"], tool_hits=[])

    chunks = [
        c async for c in chat_service.stream_chat(
            None, ChatRequest(query="公文格式要求是什么", deep_reason=False)
        )
    ]
    events = _parse_sse(chunks)
    types = [e["type"] for e in events]
    assert not any(t.startswith("react_") for t in types), "关闭后不应有任何 react 事件"
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["intent"] == "direct_qa"


@pytest.mark.asyncio
async def test_stream_chat_chitchat_bypasses_react(monkeypatch):
    """闲聊意图不受深度推理接管（默认全开也不进 TAO 循环）。"""
    _install_base(monkeypatch)
    _install_models(monkeypatch, script=["unused"], tool_hits=[])

    chunks = [c async for c in chat_service.stream_chat(None, ChatRequest(query="你好"))]
    types = [e["type"] for e in _parse_sse(chunks)]
    assert not any(t.startswith("react_") for t in types)
    meta = next(e for e in _parse_sse(chunks) if e["type"] == "meta")
    assert meta["intent"] == "chitchat"


@pytest.mark.asyncio
async def test_stream_chat_react_no_materials_completes_task(monkeypatch):
    """红线（用户 2026-10-16 要求）：零材料**不拒答**，必须完成任务。

    模型自主判断无需查库直接 finish → 零引用 → 仍走最终生成（通用知识
    兜底分支），答案末尾统一附「AI 生成，请人工鉴别」声明。
    同时验证**不做强制兜底检索**（模型没调检索就不该偷偷再检索一次）。
    """
    _install_base(monkeypatch)
    _install_models(
        monkeypatch,
        script=[
            json.dumps({"restate": "公文格式常识问题", "needs_knowledge": False,
                        "needs_knowledge_reason": "属通用格式常识"}),
            json.dumps({"thought": "这是通用公文格式常识，无需查库，直接作答",
                        "action": {"tool": "finish", "query": ""}}),
        ],
        tool_hits=[],  # 模型未调用任何工具 → 零引用
    )

    # 若策略层偷偷做兜底检索，这里会被计数（断言为 0）
    calls = {"n": 0}

    async def fake_retrieve(db, req):
        calls["n"] += 1
        return SimpleNamespace(
            citations=[Citation(chunk_id="x", document_id="d", title="《不该出现》", snippet="s", score=0.9)],
            truncated_by_security=False, top_vector_distance=0.2, keyword_hits=1, trace=None,
        )

    monkeypatch.setattr(chat_service, "retrieve_enhanced", fake_retrieve)

    chunks = [c async for c in chat_service.stream_chat(None, ChatRequest(query="公文标题怎么写"))]
    events = _parse_sse(chunks)
    done = next(e for e in events if e["type"] == "done")

    assert calls["n"] == 0, "模型未调用检索时不应做强制兜底检索"
    assert done["no_result"] is False, "零材料不等于拒答"
    assert done["disclaimer"] is True, "答案必须附 AI 生成声明"
    assert done["answer"].strip(), "必须给出实际答案（基于通用知识完成任务）"
    meta = next(e for e in events if e["type"] == "meta")
    assert not meta["citations"], "模型没调用工具就不该有引用"
    # 事件序列里能看到"模型自主决定不查库直接作答"的思考痕迹
    thoughts = " ".join(str(e.get("text") or "") for e in events if e["type"] == "thought")
    assert "无需查库" in thoughts


@pytest.mark.asyncio
async def test_stream_chat_react_decision_fail_still_completes(monkeypatch):
    """决策连续失败（零引用）也**不拒答**：直接进最终生成完成任务。"""
    saved = _install_base(monkeypatch)
    _install_models(
        monkeypatch,
        script=[
            json.dumps({"restate": "审批流程", "needs_knowledge": True}),
            RuntimeError("决策接口抖动"),
            RuntimeError("决策接口抖动"),
        ],
        tool_hits=[],
    )

    calls = {"n": 0}

    async def fake_retrieve(db, req):
        calls["n"] += 1
        return SimpleNamespace(
            citations=[Citation(chunk_id="fb", document_id="d", title="《兜底》", snippet="s", score=0.7)],
            truncated_by_security=False, top_vector_distance=0.3, keyword_hits=1, trace=None,
        )

    monkeypatch.setattr(chat_service, "retrieve_enhanced", fake_retrieve)

    events = _parse_sse([
        c async for c in chat_service.stream_chat(None, ChatRequest(query="审批流程是什么"))
    ])
    end = next(e for e in events if e["type"] == "react_end")
    assert end["stop_reason"] == "decision_fail"
    assert calls["n"] == 0, "决策失败也不应触发强制兜底检索（模型没要求检索）"
    done = next(e for e in events if e["type"] == "done")
    assert done["no_result"] is False
    assert done["disclaimer"] is True
    assert done["answer"].strip(), "决策失败也必须给出答案，不允许失败给用户"
    # 助手消息仍应落库（含失败痕迹的 TAO 文本）
    assert saved and saved[0]["intent"] == "deep_reason"