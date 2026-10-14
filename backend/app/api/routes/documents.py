"""文档接入路由：/v1/documents。

第三方公文系统按我方标准元数据接入文档；运维台（前端）通过本路由浏览真实
文档库、上传文件（带切分策略与入库进度状态机）、查看切片与统计。

路由顺序约定（FastAPI 按注册顺序匹配）：
静态路径（/stats、/jobs、/jobs/{job_id}）必须先于动态路径（/{doc_id}）注册，
否则 /documents/jobs 会被 /{doc_id} 以 doc_id="jobs" 短路为 404。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_settings_dep
from app.api.routes.spaces import (
    assert_space_writable,
    owner_of,
    visible_space_ids,
)
from app.core.config import Settings
from app.db.models import Chunk, Document, DocumentSpace, IngestionJob
from app.db.session import get_db
from app.schemas.contracts import (
    ChunkInfo,
    DocStats,
    DocumentDetail,
    DocumentIngest,
    DocumentSummary,
    IngestionJobResp,
    IngestResp,
)
from app.services.auth import Caller, get_caller
from app.services.ingestion import ingest_document, process_ingestion_job

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/ingest", response_model=IngestResp)
async def ingest(
    payload: DocumentIngest,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> IngestResp:
    """接入一篇文档（内联 content 或 content_ref），同步返回结果。

    空间所有权（2026-10-14）：指定 space_id 时必须本人可见
    （默认公共空间或本人创建），他人空间 403。
    """
    if payload.space_id:
        await assert_space_writable(db, payload.space_id, caller)
    result = await ingest_document(db, payload.model_dump())
    return IngestResp(**result)


@router.post("/upload", response_model=IngestionJobResp)
async def upload(
    file: UploadFile = File(...),
    external_id: str = Form(""),
    title: str = Form(""),
    doc_type: str = Form("通知"),
    issuer: str = Form(""),
    doc_no: str = Form(""),
    publish_date: str = Form(""),
    security_level: str = Form("INTERNAL"),
    permission_tags: str = Form(""),
    space_id: str = Form("", description="目标文档空间 ID；缺省归入默认空间"),
    chunk_strategy: str = Form("paragraph"),
    chunk_size: int = Form(800),
    chunk_overlap: int = Form(80),
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
    _: Settings = Depends(get_settings_dep),
) -> IngestionJobResp:
    """文件上传接入（PDF/DOCX），异步入库并返回任务（含状态机进度）。

    支持选择切分策略（paragraph/fixed/sentence/section）与切片尺寸，
    支持指定目标文档空间（space_id，缺省默认空间）；
    前端轮询 /documents/jobs/{id} 即可查看 PARSING→CHUNKING→EMBEDDING→INDEXED 进度。

    空间所有权（2026-10-14）：指定 space_id 时必须本人可见
    （默认公共空间或本人创建），他人空间 403。
    """
    # 入口管控：先校验空间可写（默认公共/本人空间），再落任务，
    # 避免后台任务跑到一半才在 resolve_space_id 处失败
    resolved_space = await assert_space_writable(db, space_id or None, caller)
    raw = await file.read()
    meta = {
        "external_id": external_id or file.filename or f"upload-{uuid.uuid4().hex[:8]}",
        "title": title or (file.filename or "未命名文档"),
        "filename": file.filename or "doc",
        "doc_type": doc_type,
        "issuer": issuer or None,
        "doc_no": doc_no or None,
        "publish_date": publish_date or None,
        "security_level": security_level,
        "permission_tags": [t.strip() for t in permission_tags.split(",") if t.strip()],
        "space_id": resolved_space,
        "chunk_strategy": chunk_strategy,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }
    job = IngestionJob(
        filename=file.filename or "doc",
        status="PENDING",
        strategy=chunk_strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        security_level=security_level,
        doc_type=doc_type,
        # 任务归属：列表/详情仅提交者可见（所有权隔离，2026-10-14）
        owner_type=owner_of(caller)[0],
        owner_id=owner_of(caller)[1],
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    # 后台推进状态机（独立会话，实时回写进度）
    asyncio.create_task(process_ingestion_job(job.id, raw, meta))
    return _job_resp(job)


@router.get("", response_model=list[DocumentSummary])
async def list_documents(
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
    doc_type: str | None = Query(None, description="按文种过滤"),
    security_level: str | None = Query(None, description="按密级过滤"),
    space_id: str | None = Query(None, description="按文档空间过滤"),
    status: str | None = Query(None, description="按状态过滤"),
    q: str | None = Query(None, description="标题/文号模糊检索"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
) -> list[DocumentSummary]:
    """文档库列表（管理视图，全量不分密级；真实数据，供前端展示）。

    空间所有权（2026-10-14）：仅返回本人可见空间内的文档
    （默认公共空间 + 本人创建空间）；他人空间文档不可见。
    """
    visible_ids = await visible_space_ids(db, caller)
    space_cond = (
        Document.space_id.in_(visible_ids)
        if visible_ids
        # 无可见空间时仅允许无主文档（space_id NULL，历史遗留）
        else Document.space_id.is_(None)
    )
    stmt = select(Document).where(or_(space_cond, Document.space_id.is_(None)))
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if security_level:
        stmt = stmt.where(Document.security_level == security_level)
    if space_id:
        stmt = stmt.where(Document.space_id == space_id)
    if status:
        stmt = stmt.where(Document.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Document.title.ilike(like) | Document.doc_no.ilike(like))
    stmt = stmt.order_by(Document.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
    # 空间 ID → 名称（仅本人可见空间，用于列表展示归属）
    name_map = await _visible_space_names(db, caller)
    return [
        DocumentSummary(
            id=d.id,
            external_id=d.external_id,
            title=d.title,
            doc_type=d.doc_type,
            issuer=d.issuer,
            doc_no=d.doc_no,
            security_level=d.security_level,
            permission_tags=d.permission_tags or [],
            space_id=d.space_id,
            space_name=name_map.get(d.space_id) if d.space_id else None,
            status=d.status,
            chunk_count=d.chunk_count,
            created_at=d.created_at,
        )
        for d in rows
    ]


async def _visible_space_names(db: AsyncSession, caller: Caller) -> dict[str, str]:
    """本人可见空间 ID → 名称（文档列表/详情展示归属用）。"""
    visible_ids = await visible_space_ids(db, caller)
    if not visible_ids:
        return {}
    rows = (
        await db.execute(
            select(DocumentSpace.id, DocumentSpace.name).where(
                DocumentSpace.id.in_(visible_ids)
            )
        )
    ).all()
    return {r[0]: r[1] for r in rows}


@router.get("/stats", response_model=DocStats)
async def stats(
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
    space_id: str | None = Query(None, description="按文档空间统计（可选）"),
) -> DocStats:
    """文档库统计（管理视图）：总数、切片数、按状态/密级分布。

    空间所有权（2026-10-14）：仅统计本人可见空间（默认公共 + 本人创建）。
    """
    visible_ids = await visible_space_ids(db, caller)
    space_cond = (
        Document.space_id.in_(visible_ids)
        if visible_ids
        else Document.space_id.is_(None)
    )
    doc_stmt = select(func.count()).select_from(Document).where(
        or_(space_cond, Document.space_id.is_(None))
    )
    chunk_stmt = select(func.count()).select_from(Chunk).where(
        or_(
            Chunk.space_id.in_(visible_ids) if visible_ids else Chunk.space_id.is_(None),
            Chunk.space_id.is_(None),
        )
    )
    if space_id:
        doc_stmt = doc_stmt.where(Document.space_id == space_id)
        chunk_stmt = chunk_stmt.where(Chunk.space_id == space_id)
    total_docs = (await db.execute(doc_stmt)).scalar() or 0
    total_chunks = (await db.execute(chunk_stmt)).scalar() or 0
    status_stmt = (
        select(Document.status, func.count())
        .group_by(Document.status)
        .where(or_(space_cond, Document.space_id.is_(None)))
    )
    security_stmt = (
        select(Document.security_level, func.count())
        .group_by(Document.security_level)
        .where(or_(space_cond, Document.space_id.is_(None)))
    )
    if space_id:
        status_stmt = status_stmt.where(Document.space_id == space_id)
        security_stmt = security_stmt.where(Document.space_id == space_id)
    by_status_rows = (await db.execute(status_stmt)).all()
    by_security_rows = (await db.execute(security_stmt)).all()
    return DocStats(
        total_documents=total_docs,
        total_chunks=total_chunks,
        by_status={s: c for s, c in by_status_rows},
        by_security={s: c for s, c in by_security_rows},
    )


@router.get("/jobs", response_model=list[IngestionJobResp])
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
    limit: int = Query(20, ge=1, le=100),
) -> list[IngestionJobResp]:
    """最近入库任务列表（状态机进度总览）。

    所有权隔离（2026-10-14）：仅返回本人提交的任务。
    """
    kind, owner_id, _ = owner_of(caller)
    rows = (
        await db.execute(
            select(IngestionJob)
            .where(IngestionJob.owner_type == kind, IngestionJob.owner_id == owner_id)
            .order_by(IngestionJob.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_job_resp(j) for j in rows]


@router.get("/jobs/{job_id}", response_model=IngestionJobResp)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> IngestionJobResp:
    """入库任务实时状态（前端轮询）。仅提交者本人可查（404 不暴露存在性）。"""
    job = (
        await db.execute(select(IngestionJob).where(IngestionJob.id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    kind, owner_id, _ = owner_of(caller)
    if job.owner_type != kind or job.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_resp(job)


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> DocumentDetail:
    """文档详情（含切片列表）。

    所有权隔离（2026-10-14）：他人空间内的文档对非 owner 不可见（404）。
    修复记录：曾因未导入 ChunkInfo 导致 NameError（HTTP 500），
    表现为前端"获取文档分片失败"；现已补齐导入并补充回归验证。
    """
    doc = (
        await db.execute(select(Document).where(Document.id == doc_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.space_id:
        visible = set(await visible_space_ids(db, caller))
        if doc.space_id not in visible:
            raise HTTPException(status_code=404, detail="文档不存在")
    name_map = await _visible_space_names(db, caller)
    chunk_rows = (
        await db.execute(
            select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.seq)
        )
    ).scalars().all()
    return DocumentDetail(
        id=doc.id,
        external_id=doc.external_id,
        title=doc.title,
        doc_type=doc.doc_type,
        issuer=doc.issuer,
        doc_no=doc.doc_no,
        publish_date=doc.publish_date,
        security_level=doc.security_level,
        permission_tags=doc.permission_tags or [],
        space_id=doc.space_id,
        space_name=name_map.get(doc.space_id) if doc.space_id else None,
        owner_org=doc.owner_org,
        source_system=doc.source_system,
        status=doc.status,
        chunk_count=doc.chunk_count,
        created_at=doc.created_at,
        chunks=[
            ChunkInfo(
                seq=c.seq,
                content=c.content,
                security_level=c.security_level,
                tokens=c.tokens,
            )
            for c in chunk_rows
        ],
    )


def _job_resp(j: IngestionJob) -> IngestionJobResp:
    """入库任务 ORM → 响应 DTO（含进度字段，供前端状态机展示）。"""
    return IngestionJobResp(
        id=j.id,
        filename=j.filename,
        status=j.status,
        progress=j.progress,
        total_chunks=j.total_chunks,
        processed_chunks=j.processed_chunks,
        error_message=j.error_message,
        document_id=j.document_id,
        strategy=j.strategy,
        chunk_size=j.chunk_size,
        chunk_overlap=j.chunk_overlap,
        security_level=j.security_level,
        created_at=j.created_at,
        updated_at=j.updated_at,
    )
