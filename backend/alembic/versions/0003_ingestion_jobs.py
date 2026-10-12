"""入库任务状态机表 ingestion_jobs（支撑前端入库进度展示）。

Revision ID: 0003_ingestion_jobs
Revises: 0002_search_vec_trigger

背景：文档上传/接入改为异步任务，需落库任务状态（PENDING→PARSING→CHUNKING
→EMBEDDING→INDEXED/FAILED）与进度，前端轮询展示。表结构与 app/db/models.py
的 IngestionJob 对齐（含 TimestampMixin 的 created_at/updated_at）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ingestion_jobs"
down_revision: str | None = "0002_search_vec_trigger"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'PENDING'")
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("document_id", sa.String(32), nullable=True),
        sa.Column("strategy", sa.String(16), nullable=False, server_default=sa.text("'paragraph'")),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="800"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("security_level", sa.String(16), nullable=False, server_default=sa.text("'INTERNAL'")),
        sa.Column("doc_type", sa.String(64), nullable=False, server_default=sa.text("'通知'")),
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
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_index("ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_document_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_status", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
