"""审计日志路由：/v1/audit。

运维台通过本路由查看真实操作留痕（谁 / 何时 / 做了什么 / 结果 / 主题 / 客体）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import AuditLogResp
from app.services.audit import audit_stats, list_audit_logs

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs", response_model=list[AuditLogResp])
async def get_logs(
    db: AsyncSession = Depends(get_db),
    actor: str | None = Query(None, description="按操作者过滤"),
    action: str | None = Query(None, description="按语义动作过滤"),
    object_type: str | None = Query(None, description="按客体类型过滤"),
    result: str | None = Query(None, description="按结果过滤 success/failure"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> list[AuditLogResp]:
    rows, _ = await list_audit_logs(
        db, actor=actor, action=action, object_type=object_type, result=result, page=page, size=size
    )
    return [
        AuditLogResp(
            id=r.id, actor=r.actor, actor_ip=r.actor_ip, action=r.action, method=r.method,
            endpoint=r.endpoint, subject_type=r.subject_type, subject_id=r.subject_id,
            object_type=r.object_type, object_id=r.object_id, result=r.result,
            status_code=r.status_code, detail=r.detail, duration_ms=r.duration_ms,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """审计概览：总量、近 24h 操作数、按动作/结果分布。"""
    return await audit_stats(db)
