"""会话路由：/v1/conversations。

智能问答（chat）与公文起草（draft）共用会话体系：
- 创建 / 列表 / 详情 / 重命名 / 删除；
- 手动追加消息（用户停止生成后保存部分答案等场景）；
- 多轮上下文由后端在生成时自动拼接（见 services/chat.py）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _conv_resp(c: Conversation) -> ConversationResp:
    return ConversationResp(
        id=c.id,
        title=c.title,
        conv_type=c.conv_type,
        space_ids=c.space_ids or [],
        message_count=c.message_count or 0,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _msg_resp(m: ConversationMessage) -> MessageResp:
    return MessageResp(
        id=m.id,
        role=m.role,
        content=m.content,
        citations=m.citations or [],
        sensitive_hits=m.sensitive_hits or [],
        model=m.model,
        created_at=m.created_at,
    )


@router.post("", response_model=ConversationResp, status_code=201)
async def create_conversation(
    payload: ConversationCreate, db: AsyncSession = Depends(get_db)
) -> ConversationResp:
    """创建会话（chat 问答 / draft 起草）。"""
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
    limit: int = Query(50, ge=1, le=200),
) -> list[ConversationResp]:
    """会话列表（按最后活跃时间倒序，供前端历史栏展示）。"""
    stmt = select(Conversation)
    if conv_type:
        stmt = stmt.where(Conversation.conv_type == conv_type)
    stmt = stmt.order_by(
        Conversation.last_message_at.desc().nullslast(),
        Conversation.created_at.desc(),
    ).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_conv_resp(c) for c in rows]


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
) -> ConversationResp:
    """重命名会话 / 更新绑定的知识空间。"""
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
    )
    return _msg_resp(msg)
