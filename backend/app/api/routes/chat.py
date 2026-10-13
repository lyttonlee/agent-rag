"""智能问答路由：/v1/chat（SSE 流式，多轮会话）。

与 /v1/generate（公文起草）的分工：
- /v1/chat：基于知识文档回答问题，支持多轮追问、会话历史；
- /v1/generate：按文种模板起草公文，同样支持会话（多轮修改）。

两者共用检索/生成/脱敏/审计基础设施；密级权限与空间过滤均在检索阶段下推。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import ChatRequest
from app.services.chat import stream_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/stream")
async def chat_stream(req: ChatRequest, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """智能问答流式接口（SSE）。

    conversation_id 为空时自动创建会话；多轮上下文由后端自动拼接。
    前端用 fetch + ReadableStream 消费，实时渲染；点「停止」时后端
    自动保存已生成部分为助手消息。
    """
    return StreamingResponse(
        stream_chat(db, req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )
