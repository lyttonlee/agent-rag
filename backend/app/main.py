"""FastAPI 应用入口。

模块化单体：文档接入 / 检索 / 推理 / 模板 / 导出 / 配置 / 审计 统一进程。
第三方公文系统通过 /v1/* 调用；运维台/配置中心为独立前端（frontend/）。

关键约束：
- 审计中间件必须在 ``app = FastAPI()`` 之后注册（装饰器执行时 app 必须已存在）；
- 启动时回收残留入库任务 + 种子标准模板库，保证"开箱即用"。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    audit,
    chat,
    config,
    conversations,
    documents,
    export,
    generate,
    retrieve,
    spaces,
    templates,
)
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
from app.services.audit import actor_from_request, record_audit
from app.services.ingestion import reconcile_stuck_jobs
from app.api.routes.spaces import ensure_default_space
from app.api.routes.templates import ensure_seed_templates

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("gov_doc_rag")

# 不审计这些路径（健康检查 / 文档 / OpenAPI / 审计自身，避免噪声与递归）
_SKIP_AUDIT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi", "/v1/audit")


def _client_ip(request: Request) -> str | None:
    """提取上手 IP（兼容反向代理转发的 X-Forwarded-For）。"""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：建表（dev）→ 回收残留任务 → 种子模板库 → 确保默认空间。"""
    # 开发环境自动建表（生产由 Alembic 迁移管理）
    if settings.environment in ("dev", "test"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("开发模式：已初始化数据表")
    # 启动回收：将崩溃/重启残留的非终态入库任务标记为 FAILED，避免"永久 5%"
    try:
        await reconcile_stuck_jobs()
    except Exception as exc:
        logger.warning("启动回收入库任务失败：%s", exc)
    # 模板库种子：8 类标准公文模板，保证模板库"开箱即用、非空白"
    try:
        added = await ensure_seed_templates()
        if added:
            logger.info("模板库种子初始化：新增 %d 个标准模板", added)
    except Exception as exc:
        logger.warning("模板库种子初始化失败：%s", exc)
    # 默认空间：保证任何文档都有明确空间归属（按空间检索才有意义）
    try:
        async with AsyncSessionLocal() as db:
            await ensure_default_space(db)
        logger.info("默认文档空间就绪")
    except Exception as exc:
        logger.warning("默认空间初始化失败：%s", exc)
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """审计中间件：对每个业务请求记录 谁/何时/做了什么/结果/主题/客体。

    审计为最佳努力（best-effort）：写入失败仅记日志，不影响主流程。
    注意：必须在 app 创建之后注册，否则装饰器执行时 app 尚未定义。
    """
    # 健康检查 / 文档 / 审计自身 不记录，避免噪声与递归
    path = request.url.path
    if any(path.startswith(p) for p in _SKIP_AUDIT_PREFIXES):
        return await call_next(request)

    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - start) * 1000)

    try:
        await record_audit(
            actor=actor_from_request(request),
            actor_ip=_client_ip(request),
            method=request.method,
            endpoint=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            detail=f"{request.method} {path}",
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("审计中间件写日志失败：%s", exc)
    return response


# 允许第三方网关跨域调用（生产应收紧为具体域名）
# 注意：后 add 的中间件在外层，CORS 置于审计之外，保证跨域头始终下发
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
app.include_router(spaces.router, prefix=API_PREFIX)
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(conversations.router, prefix=API_PREFIX)
app.include_router(templates.router, prefix=API_PREFIX)
app.include_router(config.router, prefix=API_PREFIX)
app.include_router(export.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)


@app.get("/health", tags=["health"])
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
    }
