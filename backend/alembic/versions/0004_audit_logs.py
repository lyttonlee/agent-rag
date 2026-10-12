"""审计日志表 audit_logs（谁/何时/做了什么/结果/主题/客体）。

Revision ID: 0004_audit_logs
Revises: 0003_ingestion_jobs

背景：系统需具备完整审计能力（《需求规格说明书》审计要求）——
每个关键操作留痕，支持按操作者/动作/客体追溯。表结构与
app/db/models.py 的 AuditLog 对齐（含 TimestampMixin）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_logs"
down_revision: str | None = "0003_ingestion_jobs"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False, server_default="anonymous"),
        sa.Column("actor_ip", sa.String(64), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("method", sa.String(16), nullable=True),
        sa.Column("endpoint", sa.String(256), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", sa.String(128), nullable=True),
        sa.Column("object_type", sa.String(32), nullable=True),
        sa.Column("object_id", sa.String(128), nullable=True),
        sa.Column("result", sa.String(16), nullable=False, server_default="success"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_subject_type", "audit_logs", ["subject_type"])
    op.create_index("ix_audit_logs_subject_id", "audit_logs", ["subject_id"])
    op.create_index("ix_audit_logs_object_type", "audit_logs", ["object_type"])
    op.create_index("ix_audit_logs_object_id", "audit_logs", ["object_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_object_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_object_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_subject_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_subject_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor", table_name="audit_logs")
    op.drop_table("audit_logs")
