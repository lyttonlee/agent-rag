"""dual granularity chunks (parent-child small-to-large)

Revision ID: 0008_dual_granularity
Revises: 0007_auth_users_api_clients
Create Date: 2026-10-13

长短切分（父子块）P1（用户 2026-10-13 需求）：
- granularity：small=检索单元（embed + tsvector）；large=生成单元（纯文本，
  向量与 tsvector 均为 NULL，不占 HNSW/GIN 索引）。存量行统一置 small，
  检索行为与迁移前完全一致（存量不管，新文档双粒度）。
- parent_id：small 切片所属 large 父块（同表自引用），NULL=无父块。
  父块删除时 SET NULL（退化为单粒度，不连带删子块）。

回滚即删两列，无数据损失（large 行随删——注意：回滚会丢失大块文本，
生产回滚前需确认无进行中的双粒度入库）。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_dual_granularity"
down_revision: str | None = "0007_auth_users_api_clients"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # granularity：server_default 保证存量行与并发写入立刻有值（NOT NULL 安全）
    op.execute(
        "ALTER TABLE chunks ADD COLUMN granularity VARCHAR(8) NOT NULL DEFAULT 'small'"
    )
    op.create_index("ix_chunks_granularity", "chunks", ["granularity"])
    # parent_id：可空自引用外键；先加列补索引后补 FK（避免大表加列时锁竞争）
    op.execute("ALTER TABLE chunks ADD COLUMN parent_id VARCHAR(32)")
    op.create_index("ix_chunks_parent_id", "chunks", ["parent_id"])
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT fk_chunks_parent "
        "FOREIGN KEY (parent_id) REFERENCES chunks(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS fk_chunks_parent")
    op.drop_index("ix_chunks_parent_id", table_name="chunks")
    op.drop_index("ix_chunks_granularity", table_name="chunks")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS parent_id")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS granularity")
