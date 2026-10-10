"""initial schema: documents / chunks / templates / config_kv（pgvector + zhparser）

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-09

设计要点（与 app/db/models.py 完全对齐）：
- chunks 表冗余 security_level 与 permission_tags，支撑检索阶段 WHERE 下推过滤。
- embedding 列使用 pgvector 的 Vector 类型（维度须与 core.config.embedding_dim 一致，当前 1024）。
- search_vec 为 tsvector 列（zhparser 中文分词），配合 GIN 索引支撑 BM25 关键词召回。
- 建表前先创建 vector / zhparser 扩展及 chinese 全文检索配置。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

# 向量维度：必须与 app.core.config.embedding_dim 保持一致，
# 改维度属于破坏性变更（需重建全部向量），切勿随意调整。
EMB_DIM = 1024

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. 扩展：pgvector（向量） + zhparser（中文分词）
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
    # 中文全文检索配置（基于 zhparser 解析器），专名召回靠自定义词典（zhparser 的
    # 自定义词典在数据库外配置，详见 docker/Dockerfile.pg 与运维文档）。
    # 注意：PG 不支持 CREATE TEXT SEARCH CONFIGURATION IF NOT EXISTS，故先 DROP 再
    # CREATE，保证迁移可重复执行且不破坏已物化的 tsvector 数据。
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese")
    op.execute("CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser)")
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION chinese "
        "ADD MAPPING FOR n, v, a, i, e, l, j, r, p, m, q, t, s, k, o, w WITH simple"
    )

    # ------------------------------------------------------------------
    # 2. documents（公文文档主表）
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(256), nullable=True),
        sa.Column("doc_no", sa.String(128), nullable=True),
        sa.Column("publish_date", sa.DateTime(), nullable=True),
        sa.Column(
            "security_level", sa.String(16), nullable=False, server_default=sa.text("'INTERNAL'")
        ),
        sa.Column(
            "permission_tags",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("owner_org", sa.String(256), nullable=True),
        sa.Column("source_system", sa.String(128), nullable=True),
        sa.Column("content_ref", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_documents_external_id", "documents", ["external_id"], unique=True)
    op.create_index("ix_documents_title", "documents", ["title"])
    op.create_index("ix_documents_doc_type", "documents", ["doc_type"])
    op.create_index("ix_documents_doc_no", "documents", ["doc_no"])
    op.create_index("ix_documents_security_level", "documents", ["security_level"])
    op.create_index("ix_documents_status", "documents", ["status"])

    # ------------------------------------------------------------------
    # 3. chunks（文档切片表 —— 检索主表）
    # ------------------------------------------------------------------
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(32),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_prefix", sa.String(512), nullable=True),
        sa.Column("embedding", Vector(EMB_DIM), nullable=True),
        sa.Column(
            "security_level", sa.String(16), nullable=False, server_default=sa.text("'INTERNAL'")
        ),
        sa.Column(
            "permission_tags",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("search_vec", TSVECTOR(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_security_level", "chunks", ["security_level"])
    # HNSW 向量索引（检索性能核心）
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    # tsvector GIN 索引（关键词召回）
    op.create_index(
        "ix_chunks_search_vec", "chunks", ["search_vec"], postgresql_using="gin"
    )

    # ------------------------------------------------------------------
    # 4. templates（文种模板库，我方维护）
    # ------------------------------------------------------------------
    op.create_table(
        "templates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("layout", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("content_ref", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_templates_doc_type", "templates", ["doc_type"], unique=True)

    # ------------------------------------------------------------------
    # 5. config_kv（配置中心 KV）
    # ------------------------------------------------------------------
    op.create_table(
        "config_kv",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("scope", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_config_kv_category", "config_kv", ["category"])
    op.create_index("ix_config_kv_key", "config_kv", ["key"])


def downgrade() -> None:
    op.drop_table("config_kv")
    op.drop_table("templates")
    op.drop_index("ix_chunks_search_vec", table_name="chunks")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
    # 中文检索配置与扩展一并清理（仅当无其他对象依赖时安全）
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese")
    # 注意：vector / zhparser 扩展默认保留（避免影响同实例其他库）；如需清理取消下一行注释
    # op.execute("DROP EXTENSION IF EXISTS zhparser")
    # op.execute("DROP EXTENSION IF EXISTS vector")
