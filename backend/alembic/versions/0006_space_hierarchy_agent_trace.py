"""空间层级（parent_id）+ 会话消息 Agent 留痕（intent / reasoning）。

Revision ID: 0006_space_hierarchy_agent_trace
Revises: 0005_spaces_conversations

背景（用户 2026-10-13 反馈）：
1. 空间管理应类似文件夹的层级关系：document_spaces 增加 parent_id
   自引用外键（根空间为 NULL），删除父空间时子空间挂起为根级
   （ON DELETE SET NULL），不级联丢数据；接口层另有"有子空间
   禁止删除"保护（409）。
2. 每轮对话的意图识别不应被后续轮次覆盖、思考过程应在回答后可
   展开查看：conversation_messages 增加 intent（意图标识）与
   reasoning（思考链全文）两列，随 assistant 消息独立留痕。
   老数据两列可空，行为向后兼容。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_space_hierarchy_agent_trace"
down_revision: str | None = "0005_spaces_conversations"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]


def upgrade() -> None:
    # ---- 1) 文档空间层级：parent_id 自引用外键 ----
    op.add_column("document_spaces", sa.Column("parent_id", sa.String(32), nullable=True))
    op.create_foreign_key(
        "fk_document_spaces_parent_id",
        "document_spaces",
        "document_spaces",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_document_spaces_parent_id", "document_spaces", ["parent_id"])

    # ---- 2) 会话消息 Agent 留痕：intent / reasoning ----
    op.add_column("conversation_messages", sa.Column("intent", sa.String(32), nullable=True))
    op.add_column("conversation_messages", sa.Column("reasoning", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_messages", "reasoning")
    op.drop_column("conversation_messages", "intent")
    op.drop_index("ix_document_spaces_parent_id", table_name="document_spaces")
    op.drop_constraint(
        "fk_document_spaces_parent_id", "document_spaces", type_="foreignkey"
    )
    op.drop_column("document_spaces", "parent_id")
