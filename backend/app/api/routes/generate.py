"""推理生成路由：/v1/generate（非流式，保留）与 /v1/generate/stream（SSE 流式）。

认证（2026-10-13）：双模认证（用户 JWT / 第三方 X-API-Key）；
生成链路内部透传请求体 max_clearance 给混合检索，因此必须在路由层
做与 /v1/retrieve、/v1/chat/stream 一致的两道管控——
1. 能力域：第三方客户端需具备 chat 域（users 不限制）；
2. 密级额度：请求声明的 max_clearance 被调用方额度压低（防超额检索涉密内容）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import GenerateRequest, GenerateResp
from app.services.audit import actor_from_request
from app.services.auth import Caller, get_caller
from app.services.generation import (
    assert_template_usable,
    generate_answer,
    stream_generate,
)

router = APIRouter(prefix="/generate", tags=["generate"])


async def _guard_caller(
    caller: Caller, req: GenerateRequest, db: AsyncSession
) -> None:
    """生成链路权限管控（与 retrieve/chat 一致）：能力域 + 密级额度压低
    + 模板所有权校验（用户 2026-10-14 需求：他人模板不可引用）。

    说明：
    1. generate_answer / stream_generate 会把 req.max_clearance 直接
       透传给 hybrid_retrieve，若不在此处压低，低额度客户端即可通过请求体
       声明高密级越权检索——这是密级过滤下推链路的入口，必须收口；
    2. SSE 流式响应头一旦发出就无法再改状态码，因此模板所有权校验
       必须提前到路由层完成（assert_template_usable）。
    """
    if not caller.allows_scope("chat"):
        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")
    req.max_clearance = caller.cap_clearance(req.max_clearance)
    await assert_template_usable(db, req, caller)


@router.post("", response_model=GenerateResp)
async def generate(
    req: GenerateRequest, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> GenerateResp:
    """检索增强生成，强制引用溯源 + 敏感词后处理（非流式）。"""
    await _guard_caller(caller, req, db)
    result = await generate_answer(db, req, caller=caller)
    return GenerateResp(**result)


@router.post("/stream")
async def generate_stream(
    req: GenerateRequest, request: Request, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式生成（SSE）：先回传检索元数据，再逐 token 返回正文，结尾脱敏。

    Agent 模式（默认开）下 meta/done 事件携带 intent 与 disclaimer
    （无材料兜底的甄别声明标志）。前端用 fetch + ReadableStream 消费，
    实时渲染，避免大草稿被判定超时。
    """
    await _guard_caller(caller, req, db)
    return StreamingResponse(
        stream_generate(db, req, actor=actor_from_request(request), caller=caller),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )
