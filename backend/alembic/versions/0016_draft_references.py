"""draft references table (上传参考文档：格式模板 / 仿写范文)

Revision ID: 0016_draft_references
Revises: 0015_security_level_six
Create Date: 2026-10-19

用户 2026-10-19 需求：起草时上传一份文档——
① 按上传文档的模板格式生成新文档；② 仿写上传文档；且用户可以
不选择已有文种/模板。

参考文档与知识库文档解耦（不切分/不向量化/不检索），独立建表：
draft_references（画像 JSON + 原文存储键 + 所有权三件套）。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0016_draft_references"
down_revision: str | None = "0015_security_level_six"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "draft_references",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, index=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(512), nullable=False),
        # 画像 JSON：layout（docx 版式）/outline/opening/closing/excerpt/
        # style_notes/para_count/approx_words
        sa.Column("profile", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PARSED"),
        sa.Column("error", sa.String(512), nullable=True),
        # 所有权（user/client；与 draft 会话归属同一套语义）
        sa.Column("owner_type", sa.String(16), nullable=False, server_default="user"),
        sa.Column("owner_id", sa.String(48), nullable=True, index=True),
        sa.Column("owner_name", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_draft_references_owner", "draft_references", ["owner_type", "owner_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_draft_references_owner", table_name="draft_references")
    op.drop_table("draft_references")
