"""文档空间路由：/v1/spaces。

文档空间是知识库的逻辑分区：文档归属空间，检索/问答可按 space_ids
（支持多个）限定范围。运维台（前端）与第三方公文系统共用本组接口：
- 第三方：创建空间 → 向指定空间接入文档（/v1/documents/ingest 带 space_id）；
- 运维台：空间管理（创建/重命名/删除）+ 按空间浏览文档。

删除保护：
- 默认空间（is_default）不可删除（存量数据归属，删了检索范围会乱）；
- 非空空间拒绝删除（409），需先迁移或删除空间内文档，避免误删知识。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, DocumentSpace
from app.db.session import get_db
from app.schemas.contracts import SpaceCreate, SpaceResp, SpaceUpdate

router = APIRouter(prefix="/spaces", tags=["spaces"])

# 默认空间固定 ID（与迁移 0005 一致，保证幂等）
DEFAULT_SPACE_ID = "spcdefault0000000000000000000"


async def ensure_default_space(db: AsyncSession) -> DocumentSpace:
    """确保默认空间存在（幂等）：不存在则创建。

    启动时调用；上传/接入未指定 space_id 时也回退到默认空间，
    保证任何文档都有明确的空间归属（检索按空间过滤才有意义）。
    """
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == DEFAULT_SPACE_ID))
    ).scalar_one_or_none()
    if row:
        return row
    row = DocumentSpace(
        id=DEFAULT_SPACE_ID,
        name="默认空间",
        code="default",
        description="存量文档归属的默认空间",
        is_default=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def resolve_space_id(db: AsyncSession, space_id: str | None) -> str:
    """把调用方传入的 space_id 收敛为有效空间 ID。

    - None/空 → 默认空间；
    - 不存在 → 404（避免把文档写进幽灵空间，后续检索按空间过滤会丢数据）。
    """
    if not space_id:
        space = await ensure_default_space(db)
        return space.id
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"文档空间不存在：{space_id}")
    return row.id


@router.post("", response_model=SpaceResp, status_code=201)
async def create_space(payload: SpaceCreate, db: AsyncSession = Depends(get_db)) -> SpaceResp:
    """创建文档空间（运维台与第三方系统共用）。"""
    # code 唯一性校验（提前报友好错误，而非等唯一索引冲突）
    dup = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.code == payload.code))
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail=f"空间编码已存在：{payload.code}")
    row = DocumentSpace(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        is_default=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _space_resp(db, row)


@router.get("", response_model=list[SpaceResp])
async def list_spaces(db: AsyncSession = Depends(get_db)) -> list[SpaceResp]:
    """空间列表（含实时文档数/切片数统计，默认空间置顶）。"""
    rows = (
        await db.execute(select(DocumentSpace).order_by(
            DocumentSpace.is_default.desc(), DocumentSpace.created_at.asc()
        ))
    ).scalars().all()
    return [await _space_resp(db, r) for r in rows]


@router.get("/{space_id}", response_model=SpaceResp)
async def get_space(space_id: str, db: AsyncSession = Depends(get_db)) -> SpaceResp:
    """空间详情（含统计）。"""
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    return await _space_resp(db, row)


@router.put("/{space_id}", response_model=SpaceResp)
async def update_space(
    space_id: str, payload: SpaceUpdate, db: AsyncSession = Depends(get_db)
) -> SpaceResp:
    """更新空间（名称/描述）。"""
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    await db.commit()
    await db.refresh(row)
    return await _space_resp(db, row)


@router.delete("/{space_id}")
async def delete_space(space_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """删除空间。

    保护策略：
    - 默认空间不可删；
    - 空间内仍有文档时拒绝删除（409），前端引导先处理文档，
      避免"删空间=丢知识"的误操作（文档的 space_id 为 SET NULL 语义，
      仅允许在确认无文档时删除）。
    """
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    if row.is_default:
        raise HTTPException(status_code=409, detail="默认空间不可删除")
    doc_count = (
        await db.execute(
            select(func.count()).select_from(Document).where(Document.space_id == space_id)
        )
    ).scalar() or 0
    if doc_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"空间内仍有 {doc_count} 篇文档，请先迁移或删除文档后再删除空间",
        )
    await db.delete(row)
    await db.commit()
    return {"deleted": space_id}


async def _space_resp(db: AsyncSession, row: DocumentSpace) -> SpaceResp:
    """ORM → 响应 DTO（实时统计文档数/切片数）。"""
    doc_count = (
        await db.execute(
            select(func.count()).select_from(Document).where(Document.space_id == row.id)
        )
    ).scalar() or 0
    chunk_count = (
        await db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.space_id == row.id)
        )
    ).scalar() or 0
    return SpaceResp(
        id=row.id,
        name=row.name,
        code=row.code,
        description=row.description,
        is_default=row.is_default,
        doc_count=doc_count,
        chunk_count=chunk_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
