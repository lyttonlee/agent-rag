"""异步数据库会话管理。

使用 SQLAlchemy 2.x 异步引擎（asyncpg 驱动），
连接串由 core.config.Settings.database_url 提供。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.pg_pool_size,
    max_overflow=_settings.pg_max_overflow,
    pool_pre_ping=True,       # 自动剔除失效连接
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with AsyncSessionLocal() as session:
        yield session
