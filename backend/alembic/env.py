"""Alembic 迁移环境配置。

- 同步引擎：使用 core.config 的 database_url_sync（postgresql+psycopg），
  因为 Alembic 默认以同步方式执行迁移。
- target_metadata：绑定 app.db.base.Base.metadata，使 `alembic check` /
  autogenerate 能与 ORM 模型对齐。
- 生产建议执行 `alembic upgrade head`；开发环境由应用自动建表，二者不冲突。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 注入应用配置与 ORM 元数据（导入 app.db.models 以注册全部表）
from app.core.config import get_settings
from app.db.base import Base

import app.db.models  # noqa: F401  确保 models 被注册到 Base.metadata

# ---- Alembic 标准配置 ----
config = context.config

# 用应用配置覆盖数据库连接串（以 .env / 环境变量为准）
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连接数据库（用于评审 / CI 比对）。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库实际执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
