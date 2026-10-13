"""用户认证（users）+ 第三方注册调用认证（api_clients）。

Revision ID: 0007_auth_users_api_clients
Revises: 0006_space_hierarchy_agent_trace

背景（用户 2026-10-13 需求：添加用户认证，尤其是第三方注册调用的认证方式）：

1. users：运维台/控制台登录账号。口令存 pbkdf2-sha256 哈希（不存明文），
   role 区分 admin/operator/viewer。
2. api_clients：第三方机器对机器调用凭证。注册时返回 api_key 明文
   （仅一次），服务端只存 sha256 哈希；scopes 限定能力域，
   max_clearance 限定可检索最高密级（与密级过滤下推联动）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_auth_users_api_clients"
down_revision: str | None = "0006_space_hierarchy_agent_trace"
branch_labels: str | Sequence[str] | None = None  # type: ignore[assignment]
depends_on: str | Sequence[str] | None = None  # type: ignore[assignment]


def upgrade() -> None:
    # ---- 1) 用户表（运维台登录账号）----
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False, server_default="operator"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    # ---- 2) 第三方客户端表（API Key 认证）----
    op.create_table(
        "api_clients",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_id", sa.String(48), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False, server_default=""),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("max_clearance", sa.String(16), nullable=False, server_default="INTERNAL"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default="self-register"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_api_clients_client_id", "api_clients", ["client_id"], unique=True)
    op.create_index("ix_api_clients_api_key_hash", "api_clients", ["api_key_hash"])


def downgrade() -> None:
    op.drop_index("ix_api_clients_api_key_hash", table_name="api_clients")
    op.drop_index("ix_api_clients_client_id", table_name="api_clients")
    op.drop_table("api_clients")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
