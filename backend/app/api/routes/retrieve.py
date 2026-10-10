"""检索路由：/v1/retrieve。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import RetrieveRequest, RetrieveResp
from app.services.retrieval import hybrid_retrieve

router = APIRouter(prefix="/retrieve", tags=["retrieve"])


@router.post("", response_model=RetrieveResp)
async def retrieve(
    req: RetrieveRequest, db: AsyncSession = Depends(get_db)
) -> RetrieveResp:
    """混合检索（关键词 + 向量 + 重排），结果已做密级/权限下推过滤。"""
    return await hybrid_retrieve(db, req)
