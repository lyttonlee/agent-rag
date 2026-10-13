"""检索路由：/v1/retrieve。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import RetrieveRequest, RetrieveResp
from app.services.auth import Caller, get_caller
from app.services.retrieval import hybrid_retrieve

router = APIRouter(prefix="/retrieve", tags=["retrieve"])


@router.post("", response_model=RetrieveResp, dependencies=[Depends(get_caller)])
async def retrieve(
    req: RetrieveRequest, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> RetrieveResp:
    """混合检索（关键词 + 向量 + 重排），结果已做密级/权限下推过滤。

    认证（2026-10-13）：双模认证；请求 max_clearance 被调用方额度压低，
    第三方客户端需具备 retrieve 能力域。
    """
    if not caller.allows_scope("retrieve"):
        raise HTTPException(status_code=403, detail="该客户端无 retrieve 能力域权限")
    req.max_clearance = caller.cap_clearance(req.max_clearance)
    return await hybrid_retrieve(db, req)
