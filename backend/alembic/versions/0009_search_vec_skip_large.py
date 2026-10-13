"""search_vec trigger skips large chunks (parent-child P1 follow-up)

Revision ID: 0009_search_vec_skip_large
Revises: 0008_dual_granularity
Create Date: 2026-10-13

0002 的兜底触发器对**所有** chunks 行生成 tsvector——但长短切分（0008）
后大块（granularity='large'）是纯文本生成单元，本就不进 BM25 索引：
1. 浪费 GIN 索引空间与写入开销（大块数量与小块同量级）；
2. 与「大块不 embed、不建索引」的设计意图相反。

本迁移让触发器对大块置空 search_vec，并清理存量 large 行。
检索层本就以 granularity='small' 显式过滤，行为无变化；
若回滚本迁移，大块会重新获得 tsvector（仅浪费空间，不影响正确性）。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_search_vec_skip_large"
down_revision: str | None = "0008_dual_granularity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # search_vec 原为 NOT NULL（0001 遗留）；大块合法为 NULL，先解除约束
    op.execute("ALTER TABLE chunks ALTER COLUMN search_vec DROP NOT NULL")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gov_trg_chunks_search_vec() RETURNS trigger AS $$
        BEGIN
            -- 大块（large）是生成单元：跳过 tsvector 生成，不进 BM25 索引
            IF NEW.granularity = 'large' THEN
                NEW.search_vec := NULL;
                RETURN NEW;
            END IF;
            NEW.search_vec := to_tsvector('chinese', COALESCE(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # 清理存量 large 行的 tsvector（双粒度上线前的历史行本应为空）
    op.execute("UPDATE chunks SET search_vec = NULL WHERE granularity = 'large'")


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gov_trg_chunks_search_vec() RETURNS trigger AS $$
        BEGIN
            NEW.search_vec := to_tsvector('chinese', COALESCE(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # 恢复 NOT NULL 前先回填（含 large 行），避免约束添加失败
    op.execute(
        "UPDATE chunks SET search_vec = to_tsvector('chinese', COALESCE(content, '')) "
        "WHERE search_vec IS NULL"
    )
    op.execute("ALTER TABLE chunks ALTER COLUMN search_vec SET NOT NULL")
