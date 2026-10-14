"""template code (multi-template per doc_type) + conversation doc_type

Revision ID: 0010_template_code_multi
Revises: 0009_search_vec_skip_large
Create Date: 2026-10-14

1. templates.code：新增模板唯一编码，支持同一文种配置多个模板
   （如多类「通知」）。doc_type 退化为文种分类（实际库中原无唯一约束，无需删除）。
   - 存量回填：按「文种_序号」（序号按 doc_type 分区、created_at 排序）生成，
     避免同文种多模板（如已有多个 doc_type='通知'）撞唯一约束；
2. conversations.doc_type：起草会话绑定文种，供历史还原时回显文种与模板。
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0010_template_code_multi"
down_revision: str | None = "0009_search_vec_skip_large"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ---- templates: 新增 code 唯一标识 ----
    op.add_column(
        "templates",
        sa.Column("code", sa.String(length=64), nullable=True),
    )
    # 存量回填：code 按「文种_序号」生成（序号按 doc_type 分区排序），
    # 即使同文种有多条（如多个 doc_type='通知'）也不会撞唯一约束。
    op.execute(
        """
        UPDATE templates t
        SET code = x.new_code
        FROM (
            SELECT id, doc_type || '_' || lpad(
                row_number() OVER (PARTITION BY doc_type ORDER BY created_at, id)::text,
                2, '0'
            ) AS new_code
            FROM templates WHERE code IS NULL
        ) x
        WHERE t.id = x.id
        """
    )
    op.execute("ALTER TABLE templates ALTER COLUMN code SET NOT NULL")
    op.create_index("ix_templates_code", "templates", ["code"], unique=True)

    # ---- conversations: 新增 doc_type（起草文种，历史还原用）----
    op.add_column(
        "conversations",
        sa.Column("doc_type", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_conversations_doc_type", "conversations", ["doc_type"])


def downgrade() -> None:
    op.drop_index("ix_conversations_doc_type", table_name="conversations")
    op.drop_column("conversations", "doc_type")
    op.drop_index("ix_templates_code", table_name="templates")
    op.drop_column("templates", "code")
