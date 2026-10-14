"""job ownership: ingestion_jobs.owner_type/owner_id

Revision ID: 0013_job_ownership
Revises: 0012_space_ownership
Create Date: 2026-10-14

补齐入库任务归属列（与 0012 的所有权隔离配套）：
- 0012 首次落地时被 --reload 的后端按中间版本提前执行（仅 document_spaces 部分），
  ingestion_jobs 部分未生效；
- 本迁移补齐两列 + 复合索引，并做存在性检查（幂等，可重复执行）。

隔离语义：入库任务列表/详情仅提交者（user.id / client.client_id）可见，
避免跨租户窥探他人入库任务的文件名与进度。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0013_job_ownership"
down_revision: str | None = "0012_space_ownership"
branch_labels: str | None = None
depends_on: str | None = None


def _columns(table: str) -> set[str]:
    """当前数据库中表的列名集合（存在性检查，保证迁移幂等）。"""
    bind = op.get_bind()
    rows = sa.inspect(bind).get_columns(table)
    return {c["name"] for c in rows}


def upgrade() -> None:
    cols = _columns("ingestion_jobs")
    if "owner_type" not in cols:
        op.add_column(
            "ingestion_jobs",
            sa.Column("owner_type", sa.String(length=16), nullable=False, server_default="user"),
        )
    if "owner_id" not in cols:
        op.add_column(
            "ingestion_jobs",
            sa.Column("owner_id", sa.String(length=48), nullable=False, server_default=""),
        )
    # 历史任务无提交者记录，统一归属 admin（任务一次性，隔离仅防跨租户窥探）
    op.execute(
        "UPDATE ingestion_jobs SET "
        "owner_type='user', "
        "owner_id=COALESCE("
        "  (SELECT u.id FROM users u WHERE u.role='admin' ORDER BY u.created_at ASC LIMIT 1), "
        "  'legacy') "
        "WHERE owner_id = '' OR owner_id IS NULL"
    )
    # 复合索引（存在则跳过，保证幂等）
    bind = op.get_bind()
    idx = {i["name"] for i in sa.inspect(bind).get_indexes("ingestion_jobs")}
    if "ix_ingestion_jobs_owner" not in idx:
        op.create_index(
            "ix_ingestion_jobs_owner", "ingestion_jobs", ["owner_type", "owner_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_owner", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "owner_id")
    op.drop_column("ingestion_jobs", "owner_type")
