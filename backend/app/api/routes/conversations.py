"""会话路由：/v1/conversations。

智能问答（chat）与公文起草（draft）共用会话体系：
- 创建 / 列表 / 详情 / 重命名 / 删除；
- 手动追加消息（用户停止生成后保存部分答案等场景）；
- 多轮上下文由后端在生成时自动拼接（见 services/chat.py）。

历史搜索（2026-10-15 用户需求）：列表端点支持两种定位方式——
`keyword` 模糊搜索（标题 + 消息内容，命中消息时返回 match_snippet
片段）与 `id` 精确查找（明确 ID 直达，不存在返回 404）。
`conversation_id` 为 `id` 的别名（2026-10-16 加固：按自然命名传参
不再静默退化为全量列表，见 _resolve_lookup_id）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.spaces import check_space_ids_visible
from app.db.models import Conversation, ConversationMessage
from app.db.session import get_db
from app.schemas.contracts import (
    ConversationCreate,
    ConversationDetail,
    ConversationResp,
    ConversationUpdate,
    MessageAppend,
    MessageResp,
)
from app.services import conversation as conv_service
from app.services.auth import Caller, get_caller

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _conv_resp(c: Conversation, match_snippet: str | None = None) -> ConversationResp:
    return ConversationResp(
        id=c.id,
        title=c.title,
        conv_type=c.conv_type,
        doc_type=c.doc_type,
        space_ids=c.space_ids or [],
        message_count=c.message_count or 0,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
        match_snippet=match_snippet,
    )


def _msg_resp(m: ConversationMessage) -> MessageResp:
    return MessageResp(
        id=m.id,
        role=m.role,
        content=m.content,
        citations=m.citations or [],
        sensitive_hits=m.sensitive_hits or [],
        model=m.model,
        intent=m.intent,
        reasoning=m.reasoning,
        created_at=m.created_at,
    )


def _escape_like(text: str) -> str:
    """转义 ILIKE 通配符（% _ \\）——用户搜索词含这些字符时按字面匹配。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_lookup_id(id: str | None, conversation_id: str | None) -> str:
    """收敛"按 ID 精确查找"的入参：id 为主，conversation_id 为别名。

    背景（用户 2026-10-16 反馈"会话ID搜索无效，依然可以搜索出所有会话"）：
    接口各处（导出/生成）都用 conversation_id 指代会话，第三方按自然命名
    传 ``?conversation_id=xxx`` 时，旧实现只认 ``id``——不认识的参数被
    静默忽略，请求退化为**无过滤条件的全量列表**，从调用方看就是
    "ID 搜索无效"。

    防御：两个名都收（以 id 为准），统一 strip；空串按未传处理
    （由调用方回落关键字/全量列表，与旧行为一致）。
    """
    return (id or conversation_id or "").strip()


def _build_conversation_stmt(*, conv_type: str | None, keyword: str, limit: int):
    """构造会话列表/搜索语句（纯函数，便于无库单测：编译级断言）。

    Args:
        conv_type: 会话类型过滤（chat/draft），空串不过滤。
        keyword: 搜索关键字（已 strip）；匹配标题 ILIKE、消息内容
            EXISTS 子查询、或整串恰好等于会话 ID。
        limit: 返回条数上限。

    Returns:
        SQLAlchemy Select（按最后活跃时间倒序）。
    """
    stmt = select(Conversation)
    if conv_type:
        stmt = stmt.where(Conversation.conv_type == conv_type)
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        # 消息内容命中用 EXISTS 相关子查询（PG 下走消息表 conversation_id 索引）
        msg_match = (
            select(ConversationMessage.id)
            .where(
                ConversationMessage.conversation_id == Conversation.id,
                ConversationMessage.content.ilike(pattern, escape="\\"),
            )
            .exists()
        )
        # 直接粘完整 ID 到关键字框也能命中（id 精确匹配并入 OR）
        stmt = stmt.where(
            or_(
                Conversation.title.ilike(pattern, escape="\\"),
                Conversation.id == keyword,
                msg_match,
            )
        )
    return stmt.order_by(
        Conversation.last_message_at.desc().nullslast(),
        Conversation.created_at.desc(),
    ).limit(limit)


def _make_snippet(content: str, keyword: str, width: int = 60) -> str:
    """从命中消息截取关键字周边片段（前端历史栏高亮用）。

    前 20 字 + 关键字 + 后 40 字的窗口；越界处用 … 标记；
    换行替换为空格（片段是单行展示的）。
    """
    pos = content.lower().find(keyword.lower())
    if pos < 0:
        return content[:width].replace("\n", " ")
    start = max(0, pos - 20)
    end = min(len(content), pos + len(keyword) + 40)
    snippet = content[start:end].replace("\n", " ")
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet += "…"
    return snippet


@router.post("", response_model=ConversationResp, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> ConversationResp:
    """创建会话（chat 问答 / draft 起草）。

    空间所有权（2026-10-14）：绑定的 space_ids 必须本人可见
    （默认公共 + 本人创建），他人空间 403。
    """
    await check_space_ids_visible(db, payload.space_ids, caller)
    row = await conv_service.create_conversation(
        db,
        conv_type=payload.conv_type,
        title=payload.title,
        space_ids=payload.space_ids,
    )
    return _conv_resp(row)


@router.get("", response_model=list[ConversationResp])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    conv_type: str | None = Query(None, description="按会话类型过滤：chat / draft"),
    keyword: str | None = Query(
        None, description="关键字模糊搜索：匹配会话标题或消息内容（命中消息时返回 match_snippet）"
    ),
    # 参数名 id 是产品要求的调用形态（第三方按"明确 ID"语义传参），
    # 刻意遮蔽内置 id——函数体内不使用内置 id，无副作用
    id: str | None = Query(
        None, description="按会话 ID 精确查找；不存在返回 404"
    ),
    # conversation_id 为 id 的别名（导出/生成等接口的自然命名）；
    # 不收敛会导致按自然命名传参时静默返回全量列表（见 _resolve_lookup_id）
    conversation_id: str | None = Query(
        None, description="id 的别名：按会话 ID 精确查找；两者都传以 id 为准"
    ),
    limit: int = Query(50, ge=1, le=200),
) -> list[ConversationResp]:
    """会话列表（按最后活跃时间倒序，供前端历史栏展示）。

    两种搜索形态（2026-10-15 用户需求）：
    - `id`（或别名 `conversation_id`）：明确 ID 精确查找，直达某条历史
      （找不到 → 404）；
    - `keyword`：模糊搜索标题与消息内容（ILIKE，通配符已转义），
      命中消息内容的会话附返 match_snippet 片段。
    两者可组合（如 id + conv_type 校验类型）；都不传即常规列表。
    """
    # 明确 ID：精确查找，不存在时 404（与"关键字无匹配"返回空列表区分）
    lookup_id = _resolve_lookup_id(id, conversation_id)
    if lookup_id:
        stmt = select(Conversation).where(Conversation.id == lookup_id)
        if conv_type:
            stmt = stmt.where(Conversation.conv_type == conv_type)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"会话不存在：{lookup_id}")
        return [_conv_resp(row)]

    keyword = (keyword or "").strip()
    stmt = _build_conversation_stmt(conv_type=conv_type, keyword=keyword, limit=limit)
    rows = (await db.execute(stmt)).scalars().all()

    # 命中消息的会话：取每条最早一条命中消息截取片段（一次查询拿全量）
    snippet_map: dict[str, str] = {}
    if keyword and rows:
        msg_rows = (
            await db.execute(
                select(
                    ConversationMessage.conversation_id,
                    ConversationMessage.content,
                )
                .where(
                    ConversationMessage.conversation_id.in_([c.id for c in rows]),
                    ConversationMessage.content.ilike(
                        f"%{_escape_like(keyword)}%", escape="\\"
                    ),
                )
                .order_by(ConversationMessage.created_at.asc())
            )
        ).all()
        for cid, content in msg_rows:
            if cid not in snippet_map:
                snippet_map[cid] = _make_snippet(content or "", keyword)

    return [_conv_resp(c, snippet_map.get(c.id)) for c in rows]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, db: AsyncSession = Depends(get_db)
) -> ConversationDetail:
    """会话详情（含全部消息，按时间正序）。"""
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
        )
    ).scalars().all()
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        conv_type=conv.conv_type,
        space_ids=conv.space_ids or [],
        message_count=conv.message_count or 0,
        last_message_at=conv.last_message_at,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=[_msg_resp(m) for m in msgs],
    )


@router.put("/{conversation_id}", response_model=ConversationResp)
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    caller: Caller = Depends(get_caller),
) -> ConversationResp:
    """重命名会话 / 更新绑定的知识空间。

    空间所有权（2026-10-14）：新绑定的 space_ids 必须本人可见，
    他人空间 403。
    """
    await check_space_ids_visible(db, payload.space_ids, caller)
    try:
        row = await conv_service.rename_conversation(
            db, conversation_id, payload.title or "", payload.space_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _conv_resp(row)


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """删除会话（消息级联删除）。"""
    ok = await conv_service.delete_conversation(db, conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": conversation_id}


@router.post("/{conversation_id}/messages", response_model=MessageResp, status_code=201)
async def append_message(
    conversation_id: str,
    payload: MessageAppend,
    db: AsyncSession = Depends(get_db),
) -> MessageResp:
    """手动追加消息。

    典型场景：用户在流式生成中点「停止」——后端 generator 被关闭时
    会自动保存已生成部分；前端也可在异常兜底时调用本接口补存。
    """
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    msg = await conv_service.append_message(
        db,
        conversation_id,
        payload.role,
        payload.content,
        citations=payload.citations,
        sensitive_hits=payload.sensitive_hits,
        model=payload.model,
        intent=payload.intent,
        reasoning=payload.reasoning,
    )
    return _msg_resp(msg)
