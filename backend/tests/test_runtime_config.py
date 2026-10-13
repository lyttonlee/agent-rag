"""运行时配置中心单测：检索参数 + 提示词模板（用户 2026-10-13 需求）。

用 SQLite 内存库仅建 ConfigKV 表（其余表含 PG 专有列无法建），
验证：种子写入、默认回退、宽松类型转换（字符串/越界/脏值）、
停用项不生效、提示词模板只收非空字符串。

运行：uv run pytest tests/test_runtime_config.py -v
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import ConfigKV
from app.services.runtime_config import (
    PROMPT_CONFIG_CATEGORY,
    RETRIEVAL_CONFIG_CATEGORY,
    RETRIEVAL_SEED,
    RetrievalConfig,
    ensure_default_configs,
    load_prompt_templates,
    load_retrieval_config,
)


@pytest_asyncio.fixture()
async def db():
    """SQLite 内存库 + 仅建 ConfigKV 表（其余表含 PG 专有列，跳过）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[ConfigKV.__table__])
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session
    await engine.dispose()


async def _put(db: AsyncSession, category: str, key: str, value, active: bool = True) -> None:
    """直接写一条配置项（模拟运维在配置中心的保存）。"""
    db.add(ConfigKV(category=category, key=key, value={"value": value}, is_active=active))
    await db.commit()


class TestRetrievalConfig:
    async def test_defaults_when_empty(self, db):
        """无任何配置 → 全部代码默认值。"""
        cfg = await load_retrieval_config(db)
        assert cfg == RetrievalConfig()

    async def test_seed_defaults(self, db):
        """启动种子写入 9 项检索配置（幂等，再调不重复）。"""
        added = await ensure_default_configs(db)
        assert added == len(RETRIEVAL_SEED)
        again = await ensure_default_configs(db)
        assert again == 0
        # 种子值即默认值
        cfg = await load_retrieval_config(db)
        assert cfg == RetrievalConfig()

    async def test_override_and_coerce(self, db):
        """字符串数字可解析；越界夹取；脏值回退默认。"""
        await _put(db, RETRIEVAL_CONFIG_CATEGORY, "final_top_n", "20")   # 字符串 → int
        await _put(db, RETRIEVAL_CONFIG_CATEGORY, "vector_top_n", 9999)  # 越界 → 夹到 500
        await _put(db, RETRIEVAL_CONFIG_CATEGORY, "rerank_enabled", "开")  # 中文布尔
        await _put(db, RETRIEVAL_CONFIG_CATEGORY, "vec_distance_threshold", "abc")  # 脏值 → 默认
        cfg = await load_retrieval_config(db)
        assert cfg.final_top_n == 20
        assert cfg.vector_top_n == 500
        assert cfg.rerank_enabled is True
        assert cfg.vec_distance_threshold == RetrievalConfig.vec_distance_threshold

    async def test_inactive_ignored(self, db):
        """停用的配置项不生效（回落默认）。"""
        await _put(db, RETRIEVAL_CONFIG_CATEGORY, "final_top_n", 3, active=False)
        cfg = await load_retrieval_config(db)
        assert cfg.final_top_n == RetrievalConfig.final_top_n

    async def test_bool_off_forms(self, db):
        """/关/false/0 均应解析为 False。"""
        for raw in (False, "关", "false", 0, "off"):
            await _put(db, RETRIEVAL_CONFIG_CATEGORY, "rerank_enabled", raw)
            cfg = await load_retrieval_config(db)
            assert cfg.rerank_enabled is False, f"raw={raw!r} 应解析为 False"


class TestPromptTemplates:
    async def test_empty_when_unset(self, db):
        """无配置 → 空字典（调用方用代码默认文案）。"""
        assert await load_prompt_templates(db) == {}

    async def test_only_nonempty_strings(self, db):
        """仅收录非空字符串；空串/非字符串忽略。"""
        await _put(db, PROMPT_CONFIG_CATEGORY, "draft_system", "自定义起草提示词")
        await _put(db, PROMPT_CONFIG_CATEGORY, "chat_system", "   ")           # 空白 → 忽略
        await _put(db, PROMPT_CONFIG_CATEGORY, "no_materials_system", 123)     # 非字符串 → 忽略
        templates = await load_prompt_templates(db)
        assert templates == {"draft_system": "自定义起草提示词"}

    async def test_inactive_ignored(self, db):
        """停用的模板不收录。"""
        await _put(db, PROMPT_CONFIG_CATEGORY, "draft_system", "x", active=False)
        assert await load_prompt_templates(db) == {}
