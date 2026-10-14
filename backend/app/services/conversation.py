"""会话服务：会话与消息的持久化（智能问答 / 公文起草共用）。

独立于路由的 service 层，供 conversations 路由与 chat 流式服务共同调用，
避免路由之间互相 import 造成循环依赖。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, ConversationMessage


async def create_conversation(
    db: AsyncSession,
    conv_type: str = "chat",
    title: str | None = None,
    space_ids: list[str] | None = None,
    doc_type: str | None = None,
) -> Conversation:
    """创建会话（标题缺省取"新会话"，首条消息到达时可再更新）。

    draft 会话可绑定 doc_type（文种），用于历史还原时回显正确的文种与模板。
    """
    row = Conversation(
        title=title or "新会话",
        conv_type=conv_type,
        space_ids=space_ids or [],
        doc_type=doc_type,
        message_count=0,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def append_message(
    db: AsyncSession,
    conversation_id: str,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    sensitive_hits: list[str] | None = None,
    model: str | None = None,
    intent: str | None = None,
    reasoning: str | None = None,
) -> ConversationMessage:
    """追加一条消息并维护会话的消息数与最后活跃时间。

    intent / reasoning（assistant 消息）：Agent 意图标识与模型思考链
    全文随消息留痕——每轮独立保存，历史回看时既能核对"这一轮判定
    的意图"，也能展开查看当时的思考过程（回答结束后不丢失）。

    标题自动衍生：会话仍是默认标题且首条消息为 user 时，
    取内容前 30 字作为标题（前端可再重命名）。
    """
    msg = ConversationMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        citations=citations or [],
        sensitive_hits=sensitive_hits or [],
        model=model,
        intent=intent,
        reasoning=reasoning,
    )
    db.add(msg)

    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if conv:
        conv.message_count = (conv.message_count or 0) + 1
        conv.last_message_at = datetime.utcnow()
        # 标题衍生：仅首条 user 消息且尚未重命名时
        if conv.title == "新会话" and role == "user" and content.strip():
            conv.title = content.strip()[:30]
    await db.commit()
    await db.refresh(msg)
    return msg


async def rename_conversation(
    db: AsyncSession, conversation_id: str, title: str, space_ids: list[str] | None = None
) -> Conversation:
    """重命名会话 / 更新绑定空间。"""
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise ValueError("会话不存在")
    conv.title = title
    if space_ids is not None:
        conv.space_ids = space_ids
    await db.commit()
    await db.refresh(conv)
    return conv


async def delete_conversation(db: AsyncSession, conversation_id: str) -> bool:
    """删除会话（消息随外键 CASCADE 一并删除）。"""
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        return False
    await db.delete(conv)
    await db.commit()
    return True


async def recent_history(
    db: AsyncSession, conversation_id: str, limit: int = 10
) -> list[ConversationMessage]:
    """取最近 N 条消息（按时间正序），用于多轮上下文拼接。

    默认 10 条（约 5 轮问答）：足够覆盖常见追问场景，
    又避免历史过长稀释检索/生成质量与撑爆上下文窗口。
    """
    rows = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def conversation_stats(db: AsyncSession, conversation_id: str) -> dict:
    """会话维度统计（消息数 / 各角色条数），供详情页展示。"""
    total = (
        await db.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        )
    ).scalar() or 0
    return {"message_count": total}
