"""推理生成路由：/v1/generate（非流式，保留）与 /v1/generate/stream（SSE 流式）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import GenerateRequest, GenerateResp
from app.services.generation import generate_answer, stream_generate

router = APIRouter(prefix="/generate", tags=["generate"])


@router.post("", response_model=GenerateResp)
async def generate(
    req: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> GenerateResp:
    """检索增强生成，强制引用溯源 + 敏感词后处理（非流式）。"""
    result = await generate_answer(db, req)
    return GenerateResp(**result)


@router.post("/stream")
async def generate_stream(
    req: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    """流式生成（SSE）：先回传检索元数据，再逐 token 返回正文，结尾脱敏。

    前端用 fetch + ReadableStream 消费，实时渲染，避免大草稿被判定超时。
    """
    return StreamingResponse(
        stream_generate(db, req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )
