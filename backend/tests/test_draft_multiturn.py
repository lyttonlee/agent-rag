"""公文起草多轮修改单测（用户 2026-10-16 硬性要求）。

背景：同一会话内再次提修改意见时，此前只把本轮 query + 检索材料交给
模型，上一稿完全丢失——"把标题改短一点"会重新生成一份完整文稿。
修复：_load_draft_history 注入往轮对话，build_generation_messages
把历史排进 messages 并追加"在上一稿基础上修改、输出完整文稿"规则。

覆盖：
- build_generation_messages：无历史（旧行为不变）/ 有历史（注入 + 规则）/
  截断上限 / 自定义模板下规则仍生效 / no_materials 组合；
- _load_draft_history：排除本轮 user 消息 / 无会话返空 / 透传 limit。

运行：uv run pytest tests/test_draft_multiturn.py -v
"""

from __future__ import annotations

import pytest

from app.schemas.contracts import Citation, GenerateRequest
from app.services import generation
from app.services.generation_prompt import (
    _DRAFT_HISTORY_ASSISTANT_MAX,
    _DRAFT_HISTORY_USER_MAX,
    build_generation_messages,
)


def _req(**kw) -> GenerateRequest:
    """构造起草请求（query 必填，其余按场景覆盖）。"""
    base = {"query": "把标题改短一点"}
    base.update(kw)
    return GenerateRequest(**base)


def _cit() -> list[Citation]:
    return [
        Citation(
            chunk_id="c1",
            document_id="d1",
            title="安全生产通知",
            doc_no="川办发36号",
            snippet="覆盖矿山、危化品、交通等重点行业",
            score=0.9,
        )
    ]


# ---- 提示词构造：多轮历史注入 ----


def test_no_history_keeps_single_turn_shape():
    """无历史（首轮起草）：消息结构退化为 system + user，不含修订规则。"""
    msgs = build_generation_messages(_req(), _cit())
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "【多轮修改】" not in msgs[0]["content"]
    assert "【用户问题】" in msgs[-1]["content"]


def test_history_injected_in_order():
    """有历史：system → 历史（正序）→ 本轮 user，角色交替不串。"""
    history = [
        ("user", "起草一份安全生产大检查的通知"),
        ("assistant", "# 关于开展安全生产大检查的通知\n\n各省、自治区、直辖市人民政府："),
    ]
    msgs = build_generation_messages(_req(), _cit(), history=history)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    # 历史内容原样进入对应消息
    assert msgs[1]["content"] == history[0][1]
    assert msgs[2]["content"] == history[1][1]
    # 本轮 user 消息携带检索材料 + 当前修改意见
    assert "【检索材料】" in msgs[3]["content"]
    assert "把标题改短一点" in msgs[3]["content"]


def test_revision_rule_appended_to_system():
    """有历史时 system 追加修订规则：上一稿基础上改、输出完整文稿。"""
    msgs = build_generation_messages(
        _req(), _cit(), history=[("user", "旧要求"), ("assistant", "上一稿全文")]
    )
    system = msgs[0]["content"]
    assert "【多轮修改】" in system
    assert "以上一稿为基础" in system
    assert "完整的修改后文稿" in system


def test_revision_rule_survives_custom_template():
    """运维自定义 draft_system 后，修订规则仍追加（配置只覆盖正文指令）。"""
    custom = {"draft_system": "你是自定义的公文引擎，只按我的规矩来。"}
    msgs = build_generation_messages(
        _req(), _cit(), templates=custom,
        history=[("assistant", "上一稿")],
    )
    assert msgs[0]["content"].startswith("你是自定义的公文引擎")
    assert "【多轮修改】" in msgs[0]["content"]


def test_revision_rule_with_no_materials():
    """no_materials（无检索材料兜底起草）+ 历史：两类指令同时生效。"""
    msgs = build_generation_messages(
        _req(), [], no_materials=True, history=[("assistant", "上一稿")]
    )
    assert "未检索到知识库参考材料" in msgs[0]["content"]
    assert "【多轮修改】" in msgs[0]["content"]


def test_history_truncation_caps():
    """截断上限：assistant（上稿）放宽到 6000，user（意见）1000。"""
    long_draft = "稿" * 9000
    long_ask = "要" * 3000
    msgs = build_generation_messages(
        _req(), _cit(), history=[("user", long_ask), ("assistant", long_draft)]
    )
    assert len(msgs[1]["content"]) == _DRAFT_HISTORY_USER_MAX == 1000
    assert len(msgs[2]["content"]) == _DRAFT_HISTORY_ASSISTANT_MAX == 6000


def test_empty_history_list_is_single_turn():
    """空列表按首轮处理（调用方传 [] 不触发多轮分支）。"""
    msgs = build_generation_messages(_req(), _cit(), history=[])
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "【多轮修改】" not in msgs[0]["content"]


# ---- 服务助手：历史加载 ----


class _FakeMsg:
    """最小消息桩（recent_history 的返回形态）。"""

    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


@pytest.mark.asyncio
async def test_load_history_excludes_current_query(monkeypatch):
    """本轮 user 消息（刚落库）不重复进入上下文；往轮原样保留。"""
    captured: dict = {}

    async def fake_recent(db, conversation_id, limit=10):
        captured["limit"] = limit
        return [
            _FakeMsg("user", "起草一份通知"),
            _FakeMsg("assistant", "第一稿"),
            _FakeMsg("user", "把标题改短一点"),  # 本轮（刚 append，需排除）
        ]

    monkeypatch.setattr(generation.conv_service, "recent_history", fake_recent)
    out = await generation._load_draft_history(None, "conv1", "把标题改短一点")
    assert out == [("user", "起草一份通知"), ("assistant", "第一稿")]
    assert captured["limit"] == generation._DRAFT_HISTORY_LIMIT


@pytest.mark.asyncio
async def test_load_history_no_conversation_returns_empty():
    """无会话 ID（首轮 / 老调用方不传）→ 空历史，行为与旧版一致。"""
    assert await generation._load_draft_history(None, None, "任何问题") == []
    assert await generation._load_draft_history(None, "", "任何问题") == []


@pytest.mark.asyncio
async def test_load_history_first_turn_is_empty(monkeypatch):
    """首轮：会话里只有刚写入的本轮 user 消息 → 过滤后为空。"""
    async def fake_recent(db, conversation_id, limit=10):
        return [_FakeMsg("user", "起草一份通知")]

    monkeypatch.setattr(generation.conv_service, "recent_history", fake_recent)
    out = await generation._load_draft_history(None, "conv1", "起草一份通知")
    assert out == []
