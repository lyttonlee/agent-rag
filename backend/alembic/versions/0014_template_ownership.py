"""template ownership: templates.owner_type/owner_id/owner_name

Revision ID: 0014_template_ownership
Revises: 0013_job_ownership
Create Date: 2026-10-14

模板管理按所有者隔离（用户 2026-10-14 需求，与空间所有权 0012 同一套语义）：
- templates 增加 owner_type/owner_id/owner_name 三列 + 复合索引；
- 存量模板（启动种子 8 类标准公文模板）统一回填为 system 公共模板——
  所有人可见、可引用起草，仅 admin 可维护，保证既有行为不回退；
- 此后经 /v1/templates 上传/新建的模板归属调用方（user/client），
  仅创建者本人可见、可编辑、可删除、可引用。

幂等：列/索引均做存在性检查，可重复执行（防 --reload 中间态问题）。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0014_template_ownership"
down_revision: str | None = "0013_job_ownership"
branch_labels: str | None = None
depends_on: str | None = None


def _columns(table: str) -> set[str]:
    """当前数据库中表的列名集合（存在性检查，保证迁移幂等）。"""
    bind = op.get_bind()
    rows = sa.inspect(bind).get_columns(table)
    return {c["name"] for c in rows}


def upgrade() -> None:
    cols = _columns("templates")
    if "owner_type" not in cols:
        # server_default="system"：存量行随加列自动回填为 system（公共模板），
        # 与种子模板语义一致；新行由 ORM 显式指定 owner。
        op.add_column(
            "templates",
            sa.Column("owner_type", sa.String(length=16), nullable=False, server_default="system"),
        )
    if "owner_id" not in cols:
        op.add_column(
            "templates",
            sa.Column("owner_id", sa.String(length=48), nullable=True),
        )
    if "owner_name" not in cols:
        op.add_column(
            "templates",
            sa.Column("owner_name", sa.String(length=128), nullable=True),
        )
    # 防御性回填：system 模板的 owner_id/owner_name 必须为 NULL（公共语义）
    op.execute(
        "UPDATE templates SET owner_id = NULL, owner_name = NULL "
        "WHERE owner_type = 'system'"
    )
    # 复合索引（存在则跳过，保证幂等）
    bind = op.get_bind()
    idx = {i["name"] for i in sa.inspect(bind).get_indexes("templates")}
    if "ix_templates_owner" not in idx:
        op.create_index("ix_templates_owner", "templates", ["owner_type", "owner_id"])


def downgrade() -> None:
    op.drop_index("ix_templates_owner", table_name="templates")
    op.drop_column("templates", "owner_name")
    op.drop_column("templates", "owner_id")
    op.drop_column("templates", "owner_type")
