"""drop stale unique index on templates.doc_type (multi-template support)

Revision ID: 0011_drop_doctype_unique
Revises: 0010_template_code_multi
Create Date: 2026-10-14

背景：早期模型曾将 templates.doc_type 设为 unique=True，并因此创建了唯一索引
ix_templates_doc_type。后续 redesign 把 doc_type 改为「文种分类（非唯一）」、
唯一性改由 code 承担（支持同一文种配置多个模板，如多类「通知」），但**漏写迁移
删除旧的唯一索引**——导致新建同文种第二个模板时 INSERT 撞唯一约束 500。

本迁移：删除 ix_templates_doc_type 的唯一性，重建为非唯一索引（与模型
doc_type index=True 对齐，保留查询性能），从而放开同文种多模板。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_drop_doctype_unique"
down_revision: str | None = "0010_template_code_multi"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 先删旧的唯一索引，再以同名重建为非唯一索引（匹配模型 index=True）
    op.drop_index("ix_templates_doc_type", table_name="templates")
    op.create_index("ix_templates_doc_type", "templates", ["doc_type"])


def downgrade() -> None:
    op.drop_index("ix_templates_doc_type", table_name="templates")
    op.create_index("ix_templates_doc_type", "templates", ["doc_type"], unique=True)
