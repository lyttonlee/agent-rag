"""推理生成路由：/v1/generate。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import GenerateRequest, GenerateResp
from app.services.generation import generate_answer

router = APIRouter(prefix="/generate", tags=["generate"])


@router.post("", response_model=GenerateResp)
async def generate(
    req: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> GenerateResp:
    """检索增强生成，强制引用溯源 + 敏感词后处理。"""
    return await generate_answer(db, req)
