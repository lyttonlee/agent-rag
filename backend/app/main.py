"""FastAPI 应用入口。

模块化单体：文档接入 / 检索 / 推理 / 模板 / 导出 / 配置 统一进程。
第三方公文系统通过 /v1/* 调用；运维台/配置中心为独立前端（frontend/）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import config, documents, export, generate, retrieve, templates
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("gov_doc_rag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 开发环境自动建表（生产由 Alembic 迁移管理）
    if settings.environment in ("dev", "test"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("开发模式：已初始化数据表")
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# 允许第三方网关跨域调用（生产应收紧为具体域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = settings.api_prefix
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(retrieve.router, prefix=API_PREFIX)
app.include_router(generate.router, prefix=API_PREFIX)
app.include_router(templates.router, prefix=API_PREFIX)
app.include_router(config.router, prefix=API_PREFIX)
app.include_router(export.router, prefix=API_PREFIX)


@app.get("/health", tags=["health"])
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
    }
