"""文档接入路由：/v1/documents。

第三方按我方标准元数据接入文档；支持内联正文与文件上传两种形态。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_settings_dep
from app.core.config import Settings
from app.db.session import get_db
from app.schemas.contracts import DocumentIngest, IngestResp
from app.services.ingestion import ingest_document

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/ingest", response_model=IngestResp)
async def ingest(payload: DocumentIngest, db: AsyncSession = Depends(get_db)) -> IngestResp:
    """接入一篇文档（内联 content 或 content_ref）。"""
    result = await ingest_document(db, payload.model_dump())
    return IngestResp(**result)


@router.post("/upload", response_model=IngestResp)
async def upload(
    file: UploadFile = File(...),
    external_id: str = "",
    title: str = "",
    doc_type: str = "通知",
    db: AsyncSession = Depends(get_db),
    _: Settings = Depends(get_settings_dep),
) -> IngestResp:
    """文件上传接入（PDF/DOCX），服务端解析正文。"""
    raw = await file.read()
    payload = {
        "external_id": external_id or file.filename,
        "title": title or file.filename,
        "doc_type": doc_type,
        "content": None,
    }
    result = await ingest_document(db, payload, raw_bytes=raw)
    return IngestResp(**result)
