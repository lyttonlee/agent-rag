"""审计日志服务：记录"谁 / 何时 / 做了什么 / 结果如何 / 主题 / 客体"。

满足《需求规格说明书》审计要求：每个关键操作留痕，便于事后追溯与合规审计。
- actor / actor_ip：操作者身份与上手 IP（信任上游网关下发 X-Operator，缺省 anonymous）。
- action：语义动作（如"文档入库""智能检索""配置变更"）。
- subject_type / subject_id：操作主体（操作者维度）。
- object_type / object_id：被操作客体（文档 / 任务 / 配置项等）。
- result / status_code / detail / duration_ms：结果与摘要。
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# 路由 → 语义动作 / 客体类型 的映射（正则按前缀匹配）
_PATH_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"^/v1/documents/upload$"), "文档入库", "document"),
    (re.compile(r"^/v1/documents/ingest$"), "文档接入", "document"),
    (re.compile(r"^/v1/documents/jobs(/.*)?$"), "入库任务查询", "ingestion_job"),
    (re.compile(r"^/v1/documents/[^/]+$"), "文档详情", "document"),
    (re.compile(r"^/v1/documents$"), "文档列表", "document"),
    (re.compile(r"^/v1/generate/stream$"), "流式智能生成", None),
    (re.compile(r"^/v1/generate$"), "智能生成", None),
    (re.compile(r"^/v1/retrieve$"), "智能检索", None),
    (re.compile(r"^/v1/templates/seed$"), "模板种子初始化", "template"),
    (re.compile(r"^/v1/templates/[^/]+$"), "模板删除", "template"),
    (re.compile(r"^/v1/templates$"), "模板维护", "template"),
    (re.compile(r"^/v1/config/items$"), "配置变更", "config"),
    (re.compile(r"^/v1/audit/logs$"), "审计日志查询", "audit_log"),
    (re.compile(r"^/v1/audit/stats$"), "审计统计查询", "audit_log"),
]


def _classify(path: str, method: str) -> tuple[str, str | None]:
    """根据路径与方法推断语义动作与客体类型。"""
    for pattern, action, obj_type in _PATH_RULES:
        if pattern.match(path):
            # 删除动作语义更精确
            if method == "DELETE" and obj_type == "template":
                return "模板删除", obj_type
            if method == "PUT" and obj_type == "config":
                return "配置更新", obj_type
            if method == "POST" and obj_type == "config":
                return "配置新增", obj_type
            if method == "POST" and obj_type == "template":
                return "模板新增", obj_type
            return action, obj_type
    return f"{method} {path}", None


def _extract_object_id(path: str) -> str | None:
    """从路径抽取资源主键（如 /v1/documents/{id}）。"""
    m = re.search(r"^/v1/(?:documents|documents/jobs|templates)/([A-Za-z0-9_]+)$", path)
    if m:
        return m.group(1)
    return None


async def record_audit(
    *,
    actor: str,
    actor_ip: str | None,
    method: str,
    endpoint: str,
    status_code: int,
    duration_ms: int | None = None,
    subject_type: str = "operator",
    subject_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    result: str | None = None,
    detail: str = "",
) -> None:
    """写入一条审计日志（失败仅记录日志，不影响主流程）。"""
    try:
        if object_type is None or object_id is None:
            action, guessed_obj = _classify(endpoint or "", method)
            object_type = object_type or guessed_obj
            object_id = object_id or _extract_object_id(endpoint or "")
        else:
            action, _ = _classify(endpoint or "", method)
        if result is None:
            result = "success" if 200 <= status_code < 400 else "failure"
        log = AuditLog(
            id=uuid.uuid4().hex,
            actor=actor,
            actor_ip=actor_ip,
            action=action,
            method=method,
            endpoint=endpoint,
            subject_type=subject_type,
            subject_id=subject_id or actor,
            object_type=object_type,
            object_id=object_id,
            result=result,
            status_code=status_code,
            detail=detail[:2000],
            duration_ms=duration_ms,
        )
        async with AsyncSessionLocal() as db:
            db.add(log)
            await db.commit()
    except Exception as exc:  # 审计写入绝不能影响业务
        logger.warning("审计日志写入失败（已忽略）：%s", exc)


async def list_audit_logs(
    db: AsyncSession,
    *,
    actor: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    result: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[AuditLog], int]:
    """分页查询审计日志（运维台使用）。"""
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
        count_stmt = count_stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if object_type:
        stmt = stmt.where(AuditLog.object_type == object_type)
        count_stmt = count_stmt.where(AuditLog.object_type == object_type)
    if result:
        stmt = stmt.where(AuditLog.result == result)
        count_stmt = count_stmt.where(AuditLog.result == result)
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (
        (await db.execute(
            stmt.order_by(AuditLog.created_at.desc()).offset((page - 1) * size).limit(size)
        ))
        .scalars()
        .all()
    )
    return list(rows), total


async def audit_stats(db: AsyncSession) -> dict:
    """运维台审计概览：总量、按动作分布、按结果分布、近 24h 操作数。"""
    from datetime import datetime, timedelta

    total = (await db.execute(select(func.count()).select_from(AuditLog))).scalar() or 0
    by_action_rows = (
        await db.execute(select(AuditLog.action, func.count()).group_by(AuditLog.action))
    ).all()
    by_result_rows = (
        await db.execute(select(AuditLog.result, func.count()).group_by(AuditLog.result))
    ).all()
    since = datetime.utcnow() - timedelta(hours=24)
    recent = (
        await db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= since)
        )
    ).scalar() or 0
    return {
        "total": total,
        "recent_24h": recent,
        "by_action": {a: c for a, c in by_action_rows},
        "by_result": {r: c for r, c in by_result_rows},
    }
