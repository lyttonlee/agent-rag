"""chunks.search_vec 自动维护：zhparser 中文分词触发器 + 历史回填。

Revision ID: 0002_search_vec_trigger
Revises: 0001_initial

背景：0001 中 chunks.search_vec 为 NOT NULL 的 tsvector 列（支撑 BM25 关键词召回）。
- 入库逻辑（ingestion）已即时生成该列值；本迁移额外增加 BEFORE INSERT/UPDATE
  触发器，作为生产环境的兜底（任何写入路径都自动维护 search_vec）。
- 同时对已存在的行做一次性回填。
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_search_vec_trigger"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]


def upgrade() -> None:
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
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_chunks_search_vec ON chunks;
        CREATE TRIGGER trg_chunks_search_vec
            BEFORE INSERT OR UPDATE OF content ON chunks
            FOR EACH ROW EXECUTE FUNCTION gov_trg_chunks_search_vec();
        """
    )
    # 回填历史数据（search_vec 为空时）
    op.execute(
        "UPDATE chunks SET search_vec = to_tsvector('chinese', COALESCE(content, '')) "
        "WHERE search_vec IS NULL OR search_vec = ''"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_chunks_search_vec ON chunks")
    op.execute("DROP FUNCTION IF EXISTS gov_trg_chunks_search_vec()")
