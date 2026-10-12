"""入库编排：解析 → 切片 → 向量化 → 落库。

MVP 阶段提供两条路径：
- ``ingest_document``：同步编排，供第三方 ``POST /documents/ingest`` 即时接入。
- ``process_ingestion_job``：异步任务，供文件上传/批量接入，按状态机
  (PENDING→PARSING→CHUNKING→EMBEDDING→INDEXED) 推进并实时回写进度，
  前端轮询即可呈现"入库进度"。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Chunk as ChunkModel
from app.db.models import Document as DocumentModel
from app.db.models import IngestionJob
from app.db.session import AsyncSessionLocal
from app.services.chunker import build_prefix, chunk_document
from app.services.model_provider import ModelProvider
from app.services.parser import parse_bytes
from app.services.storage import get_storage

_settings = get_settings()
logger = logging.getLogger(__name__)

# 非终态：这些状态在进程重启后不应"永远停留"，需回收为 FAILED。
_NON_TERMINAL = {"PENDING", "PARSING", "CHUNKING", "EMBEDDING"}


async def _embed_batched(
    provider: "ModelProvider",
    texts: list[str],
    batch_size: int = 8,
    max_retries: int = 2,
    progress_cb=None,
) -> list[list[float]]:
    """分批调用 embedding，并对超时/瞬时错误做有限重试。

    大文档切片较多，整批请求易超出内网模型响应时间；分批可限制单批规模、
    分摊风险，并在偶发超时时快速重试而不丢失已成功的批次。
    ``progress_cb(done, total)`` 用于回写入库任务进度。
    """
    out: list[list[float]] = []
    total = len(texts)
    done = 0
    for i in range(0, total, batch_size):
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
        done += len(batch)
        if progress_cb:
            await progress_cb(done, total)
    return out


async def ingest_document(
    db: AsyncSession,
    payload: dict,
    raw_bytes: bytes | None = None,
) -> dict:
    """接入一篇文档并同步入库（第三方即时接入路径）。

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
        publish_date=_parse_date(payload.get("publish_date")),
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

    # 3) 切片：按接入方指定的策略 / 尺寸切分（RAG 切分核心）
    prefix = build_prefix(doc.title, doc.issuer, payload.get("publish_date"))
    pieces = chunk_document(
        content or "",
        prefix,
        strategy=payload.get("chunk_strategy", "paragraph"),
        max_chars=payload.get("chunk_size", 800),
        overlap=payload.get("chunk_overlap", 80),
    )

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
                tokens=len(piece.content),
                # 中文 BM25 向量（zhparser）：入库时即时生成；
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


def _parse_date(value) -> datetime | None:
    """宽容解析 ISO 日期字符串为 datetime（接入方格式可能不一致）。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


async def process_ingestion_job(job_id: str, raw_bytes: bytes | None, meta: dict) -> None:
    """异步推进入库任务（状态机 + 进度回写）。

    由 ``POST /documents/upload`` 通过 asyncio.create_task 触发，运行在独立会话中，
    实时更新 ``IngestionJob`` 行；前端轮询 ``GET /documents/jobs/{id}`` 即可看到进度。

    关键修复：
    - 解析（``parse_bytes`` 为 CPU 密集型同步调用）放到线程池执行，避免阻塞事件循环，
      否则大文档解析期间轮询请求无法被服务，前端表现为"卡在解析阶段(5%)"。
    - 每个阶段补充结构化日志，便于线上定位卡点。

    Args:
        job_id: 入库任务主键
        raw_bytes: 上传文件原始字节（None 表示纯内联正文）
        meta: 接入元数据（title/external_id/doc_type/security_level/strategy/...）
    """
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(IngestionJob).where(IngestionJob.id == job_id))).scalar_one_or_none()
        if not job:
            return
        # 防御：若任务已被标记为终态（如启动回收），不再重复处理
        if job.status in ("INDEXED", "FAILED"):
            logger.warning("入库任务 %s 已是终态(%s)，跳过", job_id, job.status)
            return

        try:
            logger.info("入库任务 %s 开始：filename=%s strategy=%s", job_id, job.filename, meta.get("chunk_strategy"))

            # 阶段：解析（线程池，避免阻塞事件循环）
            job.status = "PARSING"
            job.progress = 5
            await db.commit()
            content = meta.get("content")
            if not content and raw_bytes:
                parse_name = meta.get("filename") or meta.get("title") or "doc"
                logger.info("入库任务 %s 解析中：%s (%d bytes)", job_id, parse_name, len(raw_bytes))
                parsed = await asyncio.to_thread(parse_bytes, parse_name, raw_bytes)
                content = parsed.text
                logger.info("入库任务 %s 解析完成：%d 字符，扫描件=%s", job_id, len(content or ""), parsed.is_scanned)

            # 阶段：切片
            job.status = "CHUNKING"
            job.progress = 20
            await db.commit()
            prefix = build_prefix(
                meta.get("title", ""), meta.get("issuer"), meta.get("publish_date")
            )
            pieces = chunk_document(
                content or "",
                prefix,
                strategy=meta.get("chunk_strategy", "paragraph"),
                max_chars=meta.get("chunk_size", 800),
                overlap=meta.get("chunk_overlap", 80),
            )
            job.total_chunks = len(pieces)
            logger.info("入库任务 %s 切片完成：%d 片", job_id, len(pieces))

            # 阶段：向量化（分批，逐批回写进度）
            job.status = "EMBEDDING"
            job.progress = 40
            await db.commit()
            emb_provider = ModelProvider(role="emb")
            texts = [p.content for p in pieces]

            async def _progress(done: int, total: int):
                job.processed_chunks = done
                # 40%(切片完成) → 95%(向量化完成) 线性映射
                job.progress = 40 + int(55 * done / max(total, 1))
                await db.commit()

            embeddings = await _embed_batched(
                emb_provider, texts, batch_size=8, max_retries=2, progress_cb=_progress
            )
            logger.info("入库任务 %s 向量化完成：%d 片", job_id, len(embeddings))

            # 阶段：落库文档 + 切片
            doc = DocumentModel(
                external_id=meta["external_id"],
                title=meta["title"],
                doc_type=meta.get("doc_type", "通知"),
                issuer=meta.get("issuer"),
                doc_no=meta.get("doc_no"),
                publish_date=_parse_date(meta.get("publish_date")),
                security_level=meta.get("security_level", "INTERNAL"),
                permission_tags=meta.get("permission_tags", []),
                owner_org=meta.get("owner_org"),
                source_system=meta.get("source_system"),
                status="INDEXED",
                chunk_count=len(pieces),
            )
            db.add(doc)
            await db.flush()

            if raw_bytes and not meta.get("content_ref"):
                ext = (meta.get("filename") or "bin").rsplit(".", 1)[-1].lower()
                key = f"raw/{doc.id}.{ext}"
                get_storage().save(key, raw_bytes)
                doc.content_ref = key

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
                        tokens=len(piece.content),
                        search_vec=func.to_tsvector("chinese", piece.content),
                    )
                )

            # 收尾：任务完成
            job.status = "INDEXED"
            job.progress = 100
            job.processed_chunks = len(pieces)
            job.document_id = doc.id
            await db.commit()
            logger.info("入库任务 %s 完成：document_id=%s 切片=%d", job_id, doc.id, len(pieces))
        except Exception as exc:  # 任意阶段异常 → 标记失败，不阻塞服务
            await db.rollback()
            job.status = "FAILED"
            job.error_message = f"{type(ex).__name__}: {str(ex)[:500]}"
            job.progress = 0
            await db.commit()
            logger.exception("入库任务 %s 失败：%s", job_id, exc)


async def reconcile_stuck_jobs() -> int:
    """启动回收：将残留的非终态入库任务标记为 FAILED。

    场景：进程在入库途中重启/崩溃，任务停留在 PENDING/PARSING/CHUNKING/EMBEDDING，
    若不回收会"永久 5%"。本函数在 lifespan 启动时调用，返回被回收的任务数。
    """
    async with AsyncSessionLocal() as db:
        stuck = (
            await db.execute(select(IngestionJob).where(IngestionJob.status.in_(_NON_TERMINAL)))
        ).scalars().all()
        for j in stuck:
            j.status = "FAILED"
            j.progress = 0
            j.error_message = "进程重启回收：原任务未正常完成"
        if stuck:
            await db.commit()
            logger.warning("启动回收 %d 个残留入库任务为 FAILED", len(stuck))
        return len(stuck)
