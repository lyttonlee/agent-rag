"""文档空间 + 会话历史（document_spaces / conversations / conversation_messages）。

Revision ID: 0005_spaces_conversations
Revises: 0004_audit_logs

背景（需求 V1.3）：
1. 文档库按"文档空间"分区管理：文档归属空间，检索按 space_ids 多选过滤；
   第三方系统亦可创建空间并向指定空间接入文档。
2. 智能问答 / 公文起草支持会话历史与多轮对话。

数据迁移：创建默认空间（code=default），将存量 documents/chunks 归入，
保证"按空间检索"对存量数据同样生效（切片冗余 space_id，免 JOIN 下推）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_spaces_conversations"
down_revision: str | None = "0004_audit_logs"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]

_DEFAULT_SPACE_ID = "spcdefault0000000000000000000"  # 固定 ID，便于迁移与幂等


def upgrade() -> None:
    # ---- 1) 文档空间表 ----
    op.create_table(
        "document_spaces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index("ix_document_spaces_name", "document_spaces", ["name"])
    op.create_index("ix_document_spaces_code", "document_spaces", ["code"], unique=True)
    op.create_index("ix_document_spaces_is_default", "document_spaces", ["is_default"])

    # ---- 2) documents 增加 space_id（外键 + 索引）----
    op.add_column("documents", sa.Column("space_id", sa.String(32), nullable=True))
    op.create_foreign_key(
        "fk_documents_space_id", "documents", "document_spaces", ["space_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_documents_space_id", "documents", ["space_id"])

    # ---- 3) chunks 冗余 space_id（检索切片粒度下推，免 JOIN）----
    op.add_column("chunks", sa.Column("space_id", sa.String(32), nullable=True))
    op.create_index("ix_chunks_space_id", "chunks", ["space_id"])

    # ---- 4) 会话表 ----
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False, server_default="新会话"),
        sa.Column("conv_type", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("space_ids", sa.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index("ix_conversations_conv_type", "conversations", ["conv_type"])

    # ---- 5) 会话消息表 ----
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("sensitive_hits", sa.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )
    op.create_index("ix_conversation_messages_role", "conversation_messages", ["role"])
    op.create_foreign_key(
        "fk_conversation_messages_conversation_id",
        "conversation_messages",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ---- 6) 数据迁移：默认空间 + 存量归属 ----
    op.execute(
        sa.text(
            "INSERT INTO document_spaces (id, name, code, description, is_default, created_at, updated_at) "
            f"VALUES ('{_DEFAULT_SPACE_ID}', '默认空间', 'default', '存量文档归属的默认空间', true, now(), now()) "
            "ON CONFLICT (code) DO NOTHING"
        )
    )
    op.execute(
        sa.text(
            "UPDATE documents SET space_id = :sid WHERE space_id IS NULL"
        ).bindparams(sid=_DEFAULT_SPACE_ID)
    )
    op.execute(
        sa.text(
            "UPDATE chunks SET space_id = :sid WHERE space_id IS NULL"
        ).bindparams(sid=_DEFAULT_SPACE_ID)
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversation_messages_conversation_id",
        "conversation_messages",
        type_="foreignkey",
    )
    op.drop_index("ix_conversation_messages_role", table_name="conversation_messages")
    op.drop_index(
        "ix_conversation_messages_conversation_id", table_name="conversation_messages"
    )
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_conv_type", table_name="conversations")
    op.drop_table("conversations")
    op.execute(sa.text("UPDATE chunks SET space_id = NULL"))
    op.drop_index("ix_chunks_space_id", table_name="chunks")
    op.drop_column("chunks", "space_id")
    op.drop_index("ix_documents_space_id", table_name="documents")
    op.drop_constraint("fk_documents_space_id", "documents", type_="foreignkey")
    op.drop_column("documents", "space_id")
    op.drop_index("ix_document_spaces_is_default", table_name="document_spaces")
    op.drop_index("ix_document_spaces_code", table_name="document_spaces")
    op.drop_index("ix_document_spaces_name", table_name="document_spaces")
    op.drop_table("document_spaces")
