"""配置中心路由：/v1/config。

支持敏感词、格式适配器、模型 Profile、自定义词典等可配置项。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConfigKV
from app.db.session import get_db
from app.schemas.contracts import ConfigItem

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/items")
async def list_config(
    category: str = Query(...), db: AsyncSession = Depends(get_db)
) -> list[ConfigItem]:
    rows = (
        await db.execute(select(ConfigKV).where(ConfigKV.category == category))
    ).scalars().all()
    return [
        ConfigItem(
            category=c.category, key=c.key, value=c.value, scope=c.scope, is_active=c.is_active
        )
        for c in rows
    ]


@router.put("/items")
async def upsert_config(item: ConfigItem, db: AsyncSession = Depends(get_db)) -> ConfigItem:
    existing = (
        await db.execute(
            select(ConfigKV).where(ConfigKV.category == item.category, ConfigKV.key == item.key)
        )
    ).scalar_one_or_none()
    if existing:
        existing.value = item.value
        existing.is_active = item.is_active
        existing.scope = item.scope
    else:
        existing = ConfigKV(
            category=item.category, key=item.key, value=item.value,
            scope=item.scope, is_active=item.is_active,
        )
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return ConfigItem(
        category=existing.category, key=existing.key, value=existing.value,
        scope=existing.scope, is_active=existing.is_active,
    )
