"""space ownership: document_spaces.owner_* + ingestion_jobs.owner_*

Revision ID: 0012_space_ownership
Revises: 0011_drop_doctype_unique
Create Date: 2026-10-14

空间所有权（用户 2026-10-14 需求）：
- 第三方/用户创建的空间仅创建者可见、可上传、可检索（隔离）；
- 默认空间为 system 公共空间（所有人可见/可上传）；
- 入库任务同样按提交者隔离（列表/详情仅本人可见）。

存量回填：
- is_default=true → owner_type='system'（公共）；
- 其余历史空间无创建者记录，统一归属种子 admin 用户
  （owner_type='user'，owner_id=admin.id），保证运维侧仍可管理，
  避免出现"无主空间"（任何人不可见=数据孤岛）；
- ingestion_jobs 历史任务统一归属 admin（一次性任务，无回溯价值）。
"""


import sqlalchemy as sa

from alembic import op

revision: str = "0012_space_ownership"
down_revision: str | None = "0011_drop_doctype_unique"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1) document_spaces 新增三列（owner_type 带 server_default，保证新增行有值；
    #    owner_id/owner_name 可空——system 空间无 owner_id）
    op.add_column(
        "document_spaces",
        sa.Column("owner_type", sa.String(length=16), nullable=False, server_default="user"),
    )
    op.add_column("document_spaces", sa.Column("owner_id", sa.String(length=48), nullable=True))
    op.add_column("document_spaces", sa.Column("owner_name", sa.String(length=128), nullable=True))

    # 2) 回填：默认空间 → system 公共空间
    op.execute(
        "UPDATE document_spaces SET owner_type='system', owner_id=NULL, owner_name='系统' "
        "WHERE is_default = true"
    )
    # 3) 回填：历史空间归属种子 admin（取角色为 admin 的最早用户；
    #    无 admin 的极端情况下退化为 'legacy' 标记，仍可被管理员接管）
    op.execute(
        "UPDATE document_spaces SET "
        "owner_type='user', "
        "owner_id=COALESCE("
        "  (SELECT u.id FROM users u WHERE u.role='admin' ORDER BY u.created_at ASC LIMIT 1), "
        "  'legacy'), "
        "owner_name=COALESCE("
        "  (SELECT COALESCE(NULLIF(u.display_name, ''), u.username) "
        "   FROM users u WHERE u.role='admin' ORDER BY u.created_at ASC LIMIT 1), "
        "  '历史空间') "
        "WHERE owner_type <> 'system'"
    )

    # 4) 所有权复合索引（列表/可见性过滤按 (owner_type, owner_id) 命中）
    op.create_index(
        "ix_document_spaces_owner", "document_spaces", ["owner_type", "owner_id"]
    )

    # 5) ingestion_jobs 归属列（任务列表/详情按提交者隔离）
    op.add_column(
        "ingestion_jobs",
        sa.Column("owner_type", sa.String(length=16), nullable=False, server_default="user"),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("owner_id", sa.String(length=48), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_ingestion_jobs_owner", "ingestion_jobs", ["owner_type", "owner_id"]
    )
    # 历史任务无提交者记录，统一归属 admin（任务为一次性，隔离仅防跨租户窥探）
    op.execute(
        "UPDATE ingestion_jobs SET "
        "owner_type='user', "
        "owner_id=COALESCE("
        "  (SELECT u.id FROM users u WHERE u.role='admin' ORDER BY u.created_at ASC LIMIT 1), "
        "  'legacy')"
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_owner", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "owner_id")
    op.drop_column("ingestion_jobs", "owner_type")
    op.drop_index("ix_document_spaces_owner", table_name="document_spaces")
    op.drop_column("document_spaces", "owner_name")
    op.drop_column("document_spaces", "owner_id")
    op.drop_column("document_spaces", "owner_type")
