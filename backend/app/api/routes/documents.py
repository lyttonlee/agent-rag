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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_settings_dep
from app.core.config import Settings
from app.db.models import Chunk, Document, IngestionJob
from app.db.session import get_db
from app.schemas.contracts import (
    ChunkInfo,
    DocumentDetail,
    DocumentIngest,
    DocumentSummary,
    DocStats,
    IngestResp,
    IngestionJobResp,
)
from app.services.ingestion import ingest_document, process_ingestion_job

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/ingest", response_model=IngestResp)
async def ingest(payload: DocumentIngest, db: AsyncSession = Depends(get_db)) -> IngestResp:
    """接入一篇文档（内联 content 或 content_ref），同步返回结果。"""
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
    chunk_strategy: str = Form("paragraph"),
    chunk_size: int = Form(800),
    chunk_overlap: int = Form(80),
    db: AsyncSession = Depends(get_db),
    _: Settings = Depends(get_settings_dep),
) -> IngestionJobResp:
    """文件上传接入（PDF/DOCX），异步入库并返回任务（含状态机进度）。

    支持选择切分策略（paragraph/fixed/sentence/section）与切片尺寸，
    前端轮询 /documents/jobs/{id} 即可查看 PARSING→CHUNKING→EMBEDDING→INDEXED 进度。
    """
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
    doc_type: str | None = Query(None, description="按文种过滤"),
    security_level: str | None = Query(None, description="按密级过滤"),
    status: str | None = Query(None, description="按状态过滤"),
    q: str | None = Query(None, description="标题/文号模糊检索"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
) -> list[DocumentSummary]:
    """文档库列表（管理视图，全量不分密级；真实数据，供前端展示）。"""
    stmt = select(Document)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if security_level:
        stmt = stmt.where(Document.security_level == security_level)
    if status:
        stmt = stmt.where(Document.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Document.title.ilike(like) | Document.doc_no.ilike(like))
    stmt = stmt.order_by(Document.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
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
            status=d.status,
            chunk_count=d.chunk_count,
            created_at=d.created_at,
        )
        for d in rows
    ]


@router.get("/stats", response_model=DocStats)
async def stats(db: AsyncSession = Depends(get_db)) -> DocStats:
    """文档库统计（管理视图）：总数、切片数、按状态/密级分布。"""
    total_docs = (await db.execute(select(func.count()).select_from(Document))).scalar() or 0
    total_chunks = (await db.execute(select(func.count()).select_from(Chunk))).scalar() or 0
    by_status_rows = (
        await db.execute(select(Document.status, func.count()).group_by(Document.status))
    ).all()
    by_security_rows = (
        await db.execute(
            select(Document.security_level, func.count()).group_by(Document.security_level)
        )
    ).all()
    return DocStats(
        total_documents=total_docs,
        total_chunks=total_chunks,
        by_status={s: c for s, c in by_status_rows},
        by_security={s: c for s, c in by_security_rows},
    )


@router.get("/jobs", response_model=list[IngestionJobResp])
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
) -> list[IngestionJobResp]:
    """最近入库任务列表（状态机进度总览）。"""
    rows = (
        await db.execute(
            select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [_job_resp(j) for j in rows]


@router.get("/jobs/{job_id}", response_model=IngestionJobResp)
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)) -> IngestionJobResp:
    """入库任务实时状态（前端轮询）。"""
    job = (
        await db.execute(select(IngestionJob).where(IngestionJob.id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_resp(job)


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str, db: AsyncSession = Depends(get_db)) -> DocumentDetail:
    """文档详情（含切片列表）。

    修复记录：曾因未导入 ChunkInfo 导致 NameError（HTTP 500），
    表现为前端"获取文档分片失败"；现已补齐导入并补充回归验证。
    """
    doc = (
        await db.execute(select(Document).where(Document.id == doc_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
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
