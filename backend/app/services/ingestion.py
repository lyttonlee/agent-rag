"""入库编排：解析 → 切片 → 向量化 → 落库。

MVP 阶段为同步编排（便于跟踪）；批量导入时由异步任务队列分发。
"""

from __future__ import annotations

import asyncio

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


async def _embed_batched(
    provider: "ModelProvider",
    texts: list[str],
    batch_size: int = 8,
    max_retries: int = 2,
) -> list[list[float]]:
    """分批调用 embedding，并对超时/瞬时错误做有限重试。

    大文档切片较多，整批请求易超出内网模型响应时间；分批可限制单批规模、
    分摊风险，并在偶发超时时快速重试而不丢失已成功的批次。
    """
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                out.extend(await provider.embed(batch))
                last_exc = None
                break
            except Exception as exc:  # 网络抖动/超时：退避后重试
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(2 * attempt)
        if last_exc is not None:
            raise last_exc
    return out


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
        # 优先用带扩展名的 filename 解析；无 filename 时回退 title（解析器内含 magic
        # bytes 嗅探，可兼容「无扩展名」的第三方导出文件）。
        parse_name = payload.get("filename") or payload.get("title") or "doc"
        parsed = parse_bytes(parse_name, raw_bytes)
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

    # 4) 向量化（分批调用 emb Provider + 超时重试，避免大文档单批卡死）
    emb_provider = ModelProvider(role="emb")
    texts = [p.content for p in pieces]
    embeddings = await _embed_batched(emb_provider, texts, batch_size=8, max_retries=2)

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
