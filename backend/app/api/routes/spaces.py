"""文档空间路由：/v1/spaces。

文档空间是知识库的逻辑分区：文档归属空间，检索/问答可按 space_ids
（支持多个）限定范围。运维台（前端）与第三方公文系统共用本组接口：
- 第三方：创建空间 → 向指定空间接入文档（/v1/documents/ingest 带 space_id）；
- 运维台：空间管理（创建/重命名/删除）+ 按空间浏览文档。

所有权隔离（用户 2026-10-14 需求：空间归属具象化）：
- 第三方/用户创建的空间仅创建者本人可见、可上传、可检索；
- 默认空间为 system 公共空间：所有人可见，所有人可上传；
- 他人空间在列表/详情中不可见（等同 404），上传/检索纳入时 403。

    删除保护：
    - 默认空间（is_default）不可删除（存量数据归属，删了检索范围会乱）；
    - 非空空间拒绝删除（409），需先迁移或删除空间内文档，避免误删知识；
    - 仍有子空间的空间拒绝删除（409）——层级结构下应先处理子空间。
    """

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, DocumentSpace
from app.db.session import get_db
from app.schemas.contracts import SpaceCreate, SpaceResp, SpaceUpdate
from app.services.auth import Caller, get_caller

router = APIRouter(prefix="/spaces", tags=["spaces"])

# 默认空间固定 ID（与迁移 0005 一致，保证幂等）
DEFAULT_SPACE_ID = "spcdefault0000000000000000000"


def owner_of(caller: Caller) -> tuple[str, str, str]:
    """调用方 → (owner_type, owner_id, owner_name)。

    用户 JWT → user/<user.id>；第三方 API Key → client/<client_id>。
    """
    kind = "user" if caller.kind == "user" else "client"
    return kind, caller.id, caller.name or caller.id


def is_space_owner(space: DocumentSpace, caller: Caller) -> bool:
    """当前调用方是否为该空间的创建者（默认空间不属于任何人）。"""
    if space.is_default or space.owner_type == "system":
        return False
    kind, owner_id, _ = owner_of(caller)
    return space.owner_type == kind and space.owner_id == owner_id


async def visible_space_ids(db: AsyncSession, caller: Caller) -> list[str]:
    """调用方可见的空间 ID 集合：默认（system 公共）+ 自己创建的。"""
    kind, owner_id, _ = owner_of(caller)
    rows = (
        await db.execute(
            select(DocumentSpace.id).where(
                or_(
                    DocumentSpace.is_default.is_(True),
                    (DocumentSpace.owner_type == kind)
                    & (DocumentSpace.owner_id == owner_id),
                )
            )
        )
    ).scalars().all()
    return list(rows)


async def assert_space_writable(
    db: AsyncSession, space_id: str | None, caller: Caller
) -> str:
    """校验空间可写入（上传/接入文档的入口管控）。

    - None/空 → 默认空间（公共，人人可写）；
    - 默认空间 → 放行（system 公共）；
    - 自己的空间 → 放行；
    - 他人空间 → 403（不暴露"是否存在"，与不可见语义一致）。
    返回收敛后的空间 ID。
    """
    if not space_id:
        space = await ensure_default_space(db)
        return space.id
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"文档空间不存在：{space_id}")
    if not row.is_default and not is_space_owner(row, caller):
        raise HTTPException(
            status_code=403,
            detail=f"无权向空间「{row.name}」上传文档：仅空间创建者可上传",
        )
    return row.id


async def check_space_ids_visible(
    db: AsyncSession, space_ids: list[str] | None, caller: Caller
) -> None:
    """校验请求纳入的空间均可见（检索/问答/会话绑定入口管控）。

    None/空 = 不限空间，放行（仍受密级/权限约束）；存在不可见空间 → 403。
    """
    if not space_ids:
        return
    visible = set(await visible_space_ids(db, caller))
    hidden = [s for s in space_ids if s not in visible]
    if hidden:
        raise HTTPException(
            status_code=403,
            detail=f"包含无权访问的空间（仅创建者可见）：{', '.join(hidden[:3])}"
            + (" 等" if len(hidden) > 3 else ""),
        )


async def resolve_space_id(db: AsyncSession, space_id: str | None) -> str:
    """把调用方传入的 space_id 收敛为有效空间 ID（后台入库任务用）。

    注意：可见性/可写性校验在路由入口（assert_space_writable）已完成，
    这里只做存在性收敛：
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


async def ensure_default_space(db: AsyncSession) -> DocumentSpace:
    """确保默认空间存在（幂等）：不存在则创建。

    启动时调用；上传/接入未指定 space_id 时也回退到默认空间，
    保证任何文档都有明确的空间归属（检索按空间过滤才有意义）。
    默认空间为 system 公共空间：所有人可见、可上传。
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
        owner_type="system",
        owner_id=None,
        owner_name="系统",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("", response_model=SpaceResp, status_code=201)
async def create_space(
    payload: SpaceCreate,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> SpaceResp:
    """创建文档空间（运维台与第三方系统共用；支持指定父空间形成层级）。

    所有权：空间的 owner 取自当前调用方（用户 JWT / 第三方 API Key），
    创建后仅本人可见、可上传、可检索（用户 2026-10-14 需求）。
    """
    # code 唯一性校验（提前报友好错误，而非等唯一索引冲突）
    dup = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.code == payload.code))
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail=f"空间编码已存在：{payload.code}")
    # 父空间校验（指定了就必须存在且本人可见，避免挂到他人空间下）
    if payload.parent_id:
        parent = (
            await db.execute(
                select(DocumentSpace).where(DocumentSpace.id == payload.parent_id)
            )
        ).scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail=f"父空间不存在：{payload.parent_id}")
        if not parent.is_default and not is_space_owner(parent, caller):
            raise HTTPException(
                status_code=403, detail="无权在他人空间下创建子空间（仅创建者可见）"
            )
    owner_type, owner_id, owner_name = owner_of(caller)
    row = DocumentSpace(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        is_default=False,
        parent_id=payload.parent_id,
        owner_type=owner_type,
        owner_id=owner_id,
        owner_name=owner_name,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _space_resp(db, row, caller)


@router.get("", response_model=list[SpaceResp])
async def list_spaces(
    db: AsyncSession = Depends(get_db), caller: Caller = Depends(get_caller)
) -> list[SpaceResp]:
    """空间列表（含实时文档数/切片数统计，默认空间置顶）。

    所有权过滤：仅返回默认空间（system 公共）+ 本人创建的空间；
    他人空间对本调用方不可见（用户 2026-10-14 需求）。
    """
    kind, owner_id, _ = owner_of(caller)
    rows = (
        await db.execute(
            select(DocumentSpace)
            .where(
                or_(
                    DocumentSpace.is_default.is_(True),
                    (DocumentSpace.owner_type == kind)
                    & (DocumentSpace.owner_id == owner_id),
                )
            )
            .order_by(DocumentSpace.is_default.desc(), DocumentSpace.created_at.asc())
        )
    ).scalars().all()
    return [await _space_resp(db, r, caller) for r in rows]


@router.get("/{space_id}", response_model=SpaceResp)
async def get_space(
    space_id: str,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> SpaceResp:
    """空间详情（含统计）。他人空间对非 owner 不可见（404，不暴露存在性）。"""
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    if not row.is_default and not is_space_owner(row, caller):
        raise HTTPException(status_code=404, detail="文档空间不存在")
    return await _space_resp(db, row, caller)


@router.put("/{space_id}", response_model=SpaceResp)
async def update_space(
    space_id: str,
    payload: SpaceUpdate,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> SpaceResp:
    """更新空间（名称/描述/父空间）。

    权限：仅 owner 可更新；默认空间仅 admin 可改名（保护公共空间语义）。
    移动空间（parent_id）时的防环校验：不允许把空间设为自己的父空间，
    或移动到自己的子孙节点下（否则树结构会出现环，遍历无限递归）。
    """
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    if row.is_default:
        # 默认空间（system 公共）：仅 admin 可维护名称/描述，且不允许移动
        if caller.kind != "user" or caller.role != "admin":
            raise HTTPException(status_code=403, detail="默认空间仅管理员可修改")
        if payload.parent_id is not None:
            raise HTTPException(status_code=409, detail="默认空间不支持移动")
    elif not is_space_owner(row, caller):
        raise HTTPException(status_code=403, detail="仅空间创建者可修改该空间")
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.parent_id is not None:
        # 新父级同样必须可见（默认空间或本人空间），防止挂到他人空间下
        if payload.parent_id:
            parent = (
                await db.execute(
                    select(DocumentSpace).where(DocumentSpace.id == payload.parent_id)
                )
            ).scalar_one_or_none()
            if not parent:
                raise HTTPException(status_code=404, detail=f"父空间不存在：{payload.parent_id}")
            if not parent.is_default and not is_space_owner(parent, caller):
                raise HTTPException(status_code=403, detail="无权移动到他人空间下")
        await _move_space(db, row, payload.parent_id)
    await db.commit()
    await db.refresh(row)
    return await _space_resp(db, row, caller)


async def _move_space(db: AsyncSession, row: DocumentSpace, new_parent_id: str | None) -> None:
    """把空间移动到新的父级下（含防环校验；仅改内存状态，由调用方 commit）。

    防环策略：从 new_parent_id 逐级上溯，若途中遇到 row 自身（或
    new_parent_id 就是 row 自身），说明会成环，拒绝（409）。
    """
    if new_parent_id == row.id:
        raise HTTPException(status_code=409, detail="不能把空间移动到自己的下级")
    if new_parent_id:
        parent = (
            await db.execute(
                select(DocumentSpace).where(DocumentSpace.id == new_parent_id)
            )
        ).scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail=f"父空间不存在：{new_parent_id}")
        # 上溯校验：new_parent 的祖先链中不能包含 row 自身
        cursor = parent
        while cursor is not None:
            if cursor.id == row.id:
                raise HTTPException(
                    status_code=409, detail="不能把空间移动到自己的子孙节点下"
                )
            if cursor.parent_id is None:
                break
            cursor = (
                await db.execute(
                    select(DocumentSpace).where(DocumentSpace.id == cursor.parent_id)
                )
            ).scalar_one_or_none()
    row.parent_id = new_parent_id


@router.delete("/{space_id}")
async def delete_space(
    space_id: str,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> dict:
    """删除空间（仅 owner 可删除；默认空间不可删）。

    保护策略：
    - 默认空间不可删；
    - 空间内仍有文档时拒绝删除（409），前端引导先处理文档，
      避免"删空间=丢知识"的误操作（文档的 space_id 为 SET NULL 语义，
      仅允许在确认无文档时删除）。
    """
    row = (
        await db.execute(select(DocumentSpace).where(DocumentSpace.id == space_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="文档空间不存在")
    if row.is_default:
        raise HTTPException(status_code=409, detail="默认空间不可删除")
    if not is_space_owner(row, caller):
        raise HTTPException(status_code=403, detail="仅空间创建者可删除该空间")
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
    # 层级保护：仍有子空间时拒绝删除（先处理或移动子空间，避免孤儿目录）
    child_count = (
        await db.execute(
            select(func.count())
            .select_from(DocumentSpace)
            .where(DocumentSpace.parent_id == space_id)
        )
    ).scalar() or 0
    if child_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"空间下仍有 {child_count} 个子空间，请先删除或移动子空间",
        )
    await db.delete(row)
    await db.commit()
    return {"deleted": space_id}


async def _space_resp(db: AsyncSession, row: DocumentSpace, caller: Caller) -> SpaceResp:
    """ORM → 响应 DTO（实时统计文档数/切片数 + 所有权视图字段）。"""
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
    mine = is_space_owner(row, caller)
    return SpaceResp(
        id=row.id,
        name=row.name,
        code=row.code,
        description=row.description,
        is_default=row.is_default,
        parent_id=row.parent_id,
        owner_type=row.owner_type or "user",
        owner_id=row.owner_id,
        owner_name=row.owner_name,
        is_mine=mine,
        # 可上传：默认公共空间人人可写；私有空间仅 owner
        can_upload=bool(row.is_default) or mine,
        doc_count=doc_count,
        chunk_count=chunk_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
