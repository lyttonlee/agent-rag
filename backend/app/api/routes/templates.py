"""文种模板库路由：/v1/templates。

我方维护模板库；第三方可导入提供模板样式（版式/字体/落款）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template
from app.db.session import get_db
from app.schemas.contracts import ConfigItem

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("")
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[ConfigItem]:
    rows = (await db.execute(select(Template).where(Template.is_active.is_(True)))).scalars().all()
    return [
        ConfigItem(category="template", key=t.doc_type, value={"name": t.name, "layout": t.layout})
        for t in rows
    ]


@router.post("")
async def create_template(item: ConfigItem, db: AsyncSession = Depends(get_db)) -> ConfigItem:
    t = Template(doc_type=item.key, name=item.value.get("name", item.key), layout=item.value)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return ConfigItem(category="template", key=t.doc_type, value=item.value)
