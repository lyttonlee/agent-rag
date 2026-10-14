"""智能问答路由：/v1/chat（SSE 流式，多轮会话）。

与 /v1/generate（公文起草）的分工：
- /v1/chat：基于知识文档回答问题，支持多轮追问、会话历史；
- /v1/generate：按文种模板起草公文，同样支持会话（多轮修改）。

两者共用检索/生成/脱敏/审计基础设施；密级权限与空间过滤均在检索阶段下推。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import ChatRequest
from app.services.audit import actor_from_request
from app.services.auth import Caller, get_caller
from app.services.chat import stream_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/stream", dependencies=[Depends(get_caller)])
async def chat_stream(
    req: ChatRequest, request: Request, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """智能问答流式接口（SSE）。

    conversation_id 为空时自动创建会话；多轮上下文由后端自动拼接。
    Agent 模式（默认开）下先做意图路由再分发策略，SSE 事件增加
    intent/plan/step_*（老客户端忽略新事件即可兼容）。
    前端用 fetch + ReadableStream 消费，实时渲染；点「停止」时后端
    自动保存已生成部分为助手消息。

    认证（2026-10-13）：双模认证（用户 JWT / 第三方 X-API-Key）；
    请求声明的 max_clearance 会被调用方额度压低（权限管控入口），
    第三方客户端还需具备 chat 能力域。
    空间所有权（2026-10-14）：请求/会话绑定的 space_ids 仅可纳入
    本人可见空间（默认公共 + 本人创建），他人空间 403。
    """
    if not caller.allows_scope("chat"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")
    # 空间所有权：请求显式携带的空间必须可见（会话绑定在创建/更新时已校验）
    from app.api.routes.spaces import check_space_ids_visible

    await check_space_ids_visible(db, req.space_ids, caller)
    # 权限管控：请求密级不得超过调用方额度（密级过滤下推的入口）
    req.max_clearance = caller.cap_clearance(req.max_clearance)
    return StreamingResponse(
        stream_chat(db, req, actor=actor_from_request(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )
