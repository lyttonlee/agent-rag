"""入库编排：解析 → 切片 → 向量化 → 落库。

MVP 阶段为同步编排（便于跟踪）；批量导入时由异步任务队列分发。
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Chunk as ChunkModel
from app.db.models import Document as DocumentModel
from app.services.chunker import build_prefix, chunk_text
from app.services.model_provider import ModelProvider
from app.services.parser import parse_bytes
from app.services.storage import get_storage

_settings = get_settings()


async def ingest_document(
    db: AsyncSession,
    payload: dict,
    raw_bytes: bytes | None = None,
) -> dict:
    """接入一篇文档并入库。

    Args:
        db: 异步会话
        payload: DocumentIngest 的 dict 形式（external_id/title/.../content/content_ref）
        raw_bytes: 当 content_ref 指向内联上传时的原始字节
    Returns:
        入库结果摘要
    """
    # 1) 取正文：优先内联 content，其次解析 raw_bytes
    content = payload.get("content")
    if not content and raw_bytes:
        parsed = parse_bytes(payload.get("title", "doc"), raw_bytes)
        content = parsed.text

    # 2) 写文档主表（content_ref 优先取外部托管键；后续若有 raw_bytes 则本地落盘覆盖）
    doc = DocumentModel(
        external_id=payload["external_id"],
        title=payload["title"],
        doc_type=payload["doc_type"],
        issuer=payload.get("issuer"),
        doc_no=payload.get("doc_no"),
        security_level=payload.get("security_level", "INTERNAL"),
        permission_tags=payload.get("permission_tags", []),
        owner_org=payload.get("owner_org"),
        source_system=payload.get("source_system"),
        content_ref=payload.get("content_ref"),
        status="PARSING",
    )
    db.add(doc)
    await db.flush()

    # 2.5) 持久化原文：仅当上游未托管且本次上传了 raw_bytes 时，本地落盘并记录键
    if raw_bytes and not payload.get("content_ref"):
        ext = (payload.get("filename") or "bin").rsplit(".", 1)[-1].lower()
        key = f"raw/{doc.id}.{ext}"
        get_storage().save(key, raw_bytes)
        doc.content_ref = key

    # 3) 切片 + 前缀
    prefix = build_prefix(doc.title, doc.issuer, payload.get("publish_date"))
    pieces = chunk_text(content or "", prefix)

    # 4) 向量化（批量，调用 emb Provider）
    emb_provider = ModelProvider(role="emb")
    embeddings = await emb_provider.embed([p.content for p in pieces])

    # 5) 落库切片（冗余密级/权限用于检索下推）
    for seq, (piece, vec) in enumerate(zip(pieces, embeddings), start=1):
        db.add(
            ChunkModel(
                document_id=doc.id,
                seq=seq,
                content=piece.content,
                content_prefix=prefix,
                embedding=vec,
                security_level=doc.security_level,
                permission_tags=doc.permission_tags,
                # 中文 BM25 向量（zhparser）：入库时即时生成，
                # 生产环境另有触发器兜底（见迁移 0002），确保 dev/prod 一致。
                search_vec=func.to_tsvector("chinese", piece.content),
            )
        )

    doc.chunk_count = len(pieces)
    doc.status = "INDEXED"
    await db.commit()
    await db.refresh(doc)
    return {
        "external_id": doc.external_id,
        "document_id": doc.id,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
    }
