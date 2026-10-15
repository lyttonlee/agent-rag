"""会话历史搜索单测（2026-10-15 用户需求：问答/起草历史支持关键字 + 明确 ID）。

覆盖纯函数层：ILIKE 通配符转义、命中片段截取、列表/搜索语句构造
（编译级断言——会话表含 PG 专有 ARRAY 列，SQLite 无法建表，故按
项目惯例用"编译断言 + 实库联调"双轨，实库行为由冒烟脚本覆盖）。

运行：uv run pytest tests/test_conversation_search.py -v
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.api.routes.conversations import (
    _build_conversation_stmt,
    _conv_resp,
    _escape_like,
    _make_snippet,
)
from app.db.models import Conversation


# ---- ILIKE 转义 ----


def test_escape_like_wildcards():
    """% _ \\ 按字面转义，防止用户输入被当作通配符。"""
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    assert _escape_like("C:\\path") == "C:\\\\path"
    assert _escape_like("普通关键字") == "普通关键字"


# ---- 命中片段 ----


def test_snippet_window_around_keyword():
    """片段 = 关键字前 20 字 + 关键字 + 后 40 字，越界补省略号。"""
    content = "关于核电厂退役准备管理有关工作的通知，各运营单位应当编制退役准备计划"
    snip = _make_snippet(content, "退役准备")
    assert "退役准备" in snip
    assert snip.startswith("…") is False or content.index("退役准备") <= 20
    assert len(snip) <= 20 + len("退役准备") + 40 + 2  # 两个省略号


def test_snippet_newline_flattened_and_not_found():
    """换行替换为空格；关键字不在内容中时返回开头截断。"""
    assert "\n" not in _make_snippet("第一行\n第二行含关键字\n第三行", "关键字")
    assert _make_snippet("没有命中的内容", "xyz") == "没有命中的内容"


# ---- 语句构造（编译级断言） ----


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_plain_list_has_no_search_clause():
    """无关键字：不含 ILIKE / EXISTS / id 等值条件。"""
    sql = _sql(_build_conversation_stmt(conv_type=None, keyword="", limit=50))
    assert "ILIKE" not in sql
    assert "EXISTS" not in sql
    assert "LIMIT" in sql
    assert "last_message_at" in sql  # 排序保持倒序


def test_keyword_search_covers_title_message_and_id():
    """关键字搜索：标题 ILIKE + 消息 EXISTS 子查询 + ID 等值三路并存。"""
    sql = _sql(_build_conversation_stmt(conv_type=None, keyword="退役", limit=50))
    assert "ILIKE" in sql
    assert "EXISTS" in sql
    assert "conversation_messages" in sql  # 消息内容子查询
    assert "conversations.id =" in sql    # 整串 ID 直贴也能命中


def test_keyword_escaping_in_compiled_sql():
    """通配符转义后进入 pattern（编译结果含转义参数）。"""
    stmt = _build_conversation_stmt(conv_type=None, keyword="100%", limit=10)
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert any("\\%" in str(v) for v in params.values()), "应出现转义后的 %"


def test_conv_type_filter_and_limit():
    """类型过滤 + limit 进入语句。"""
    sql = _sql(_build_conversation_stmt(conv_type="chat", keyword="", limit=7))
    assert "conv_type" in sql
    assert "LIMIT" in sql


# ---- 响应装配 ----


def test_conv_resp_includes_snippet():
    """match_snippet 透传（关键字命中消息时前端高亮用）。"""
    conv = Conversation(
        id="c1", title="测试会话", conv_type="chat", message_count=2
    )
    resp = _conv_resp(conv, "…含**关键字**的片段…")
    assert resp.match_snippet == "…含**关键字**的片段…"
    assert _conv_resp(conv).match_snippet is None  # 普通列表无片段
