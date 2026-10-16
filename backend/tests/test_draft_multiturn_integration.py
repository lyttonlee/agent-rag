"""公文起草多轮上下文——端到端编排集成测试（用户 2026-10-16 致命缺陷）。

不连真实 DB / 不调真实模型：用内存会话存储 + mock 模型，验证
stream_generate 在同一会话内第二轮起草时，确实把"上一稿"注入模型
上下文（此前丢失上一稿、重新生成一份完整文稿的根因）。

覆盖：
- 首轮：messages 不含任何往轮草稿（正确行为）；
- 第二轮（同一 conversation_id）：messages 必须包含第一轮的 assistant
  草稿正文——否则"在上一稿基础上修改"无从谈起。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.contracts import GenerateRequest
from app.services import generation


class _Msg:
    """recent_history 返回的最小消息桩（带 role / content）。"""

    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _Conv:
    id = "conv-A"


@pytest.mark.asyncio
async def test_stream_generate_injects_prior_draft_async(monkeypatch):
    """同一会话第二轮必须把上一稿喂给模型。"""
    store: dict[str, list] = {}
    captured: list = []

    async def fake_create(db, conv_type, doc_type=None, space_ids=None):
        return _Conv()

    async def fake_append(db, conversation_id, role, content, **kw):
        store.setdefault(conversation_id, []).append(_Msg(role, content))

    async def fake_recent(db, conversation_id, limit=10):
        return list(store.get(conversation_id, []))

    monkeypatch.setattr(generation.conv_service, "create_conversation", fake_create)
    monkeypatch.setattr(generation.conv_service, "append_message", fake_append)
    monkeypatch.setattr(generation.conv_service, "recent_history", fake_recent)
    # 绕过会话查询（不参与断言，统一返回同一会话 ID）
    async def _always_conv_a(db, req, doc_type=None):
        return "conv-A"
    monkeypatch.setattr(generation, "_ensure_draft_conversation", _always_conv_a)

    async def fake_retrieve(db, req):
        return SimpleNamespace(citations=[], truncated_by_security=False)

    monkeypatch.setattr(generation, "retrieve_enhanced", fake_retrieve)

    async def fake_cfg(db):
        return SimpleNamespace(vec_distance_threshold=0.6)
    monkeypatch.setattr(generation, "load_retrieval_config", fake_cfg)

    async def fake_tpl(db):
        return {}
    monkeypatch.setattr(generation, "load_prompt_templates", fake_tpl)

    async def fake_agent(db):
        return SimpleNamespace(enabled=False)
    monkeypatch.setattr(generation, "load_agent_config", fake_agent)

    async def fake_sensitive(db, req):
        return []
    monkeypatch.setattr(generation, "_resolve_sensitive_words", fake_sensitive)

    class FakeModelProvider:
        def __init__(self, role=None):
            self.role = role
            self.model_name = "fake-gen-model"

        async def generate_stream(self, messages):
            captured.append(messages)
            yield ("content", "第一稿全文：关于开展安全生产大检查的通知\n各有关部门：")

    monkeypatch.setattr(generation, "ModelProvider", FakeModelProvider)

    # ---- 第一轮：无历史，正常起草 ----
    req1 = GenerateRequest(query="起草一份安全生产大检查的通知")
    async for _ in generation.stream_generate(db=None, req=req1, actor="t"):
        pass

    # ---- 第二轮：同一会话，提出修改 ----
    req2 = GenerateRequest(query="把标题改短一点", conversation_id="conv-A")
    async for _ in generation.stream_generate(db=None, req=req2, actor="t"):
        pass

    assert len(captured) == 2, f"应捕获两轮消息，实际 {len(captured)}"
    # 首轮不应含任何往轮草稿
    assert all("第一稿全文" not in m["content"] for m in captured[0])
    # 第二轮必须把第一稿注入模型上下文（致命缺陷修复的核心断言）
    assert any(
        "第一稿全文" in m["content"] for m in captured[1]
    ), "致命缺陷未修复：第二轮未把上一稿注入模型上下文"
