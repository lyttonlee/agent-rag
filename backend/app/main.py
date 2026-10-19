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

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    audit,
    auth,
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
from app.api.routes.spaces import ensure_default_space
from app.api.routes.templates import ensure_seed_templates
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal, engine
from app.services.audit import actor_from_request, record_audit
from app.services.auth import ensure_admin_user, get_caller
from app.services.ingestion import reconcile_stuck_jobs

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("gov_doc_rag")

# 不审计这些路径（健康检查 / 文档 / OpenAPI / 审计自身 / 登录态探测，避免噪声与递归）
# 注意：登录（/v1/auth/login）与客户端注册保留审计——安全相关操作必须留痕
_SKIP_AUDIT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi", "/v1/audit", "/v1/auth/me")


def _client_ip(request: Request) -> str | None:
    """提取上手 IP（兼容反向代理转发的 X-Forwarded-For）。"""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：DB 自检（致命）→ 自动建表/更新 → 回收/种子 → 依赖自检（降级）。"""
    # 1) 数据库预检：连不上直接 fail-fast，给出可操作排障提示
    #    （而不是让 create_all 抛裸 traceback，或把问题留到首个业务请求）
    from app.services.startup_checks import check_database, log_check_results, run_startup_checks

    db_check = await check_database()
    if not db_check.ok:
        logger.error("启动中止：%s", db_check.detail)
        raise RuntimeError(f"数据库不可用，服务无法启动：{db_check.detail}")
    logger.info("数据库预检通过：%s", db_check.detail)

    # 2) 自动建表/更新：alembic upgrade head（咨询锁防多进程竞争，dev 兜底 create_all）。
    #    覆盖"创建或更新"两种语义——create_all 只能建缺失表，无法加列/改索引。
    from app.db.migrate import upgrade_schema

    await upgrade_schema()

    # 3) 启动回收：将崩溃/重启残留的非终态入库任务标记为 FAILED，避免"永久 5%"
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
    # 种子管理员：dev 开箱即登录（admin/admin123，生产用环境变量覆盖）
    try:
        async with AsyncSessionLocal() as db:
            admin = await ensure_admin_user(db)
        logger.info("管理员账号就绪：%s（角色 %s）", admin.username, admin.role)
    except Exception as exc:
        logger.warning("种子管理员初始化失败：%s", exc)
    # 配置中心种子：默认检索参数写入 ConfigKV（幂等，不覆盖用户已改值），
    # 使「配置中心 → 检索参数」开箱即见、可调（用户 2026-10-13 需求）
    try:
        from app.services.runtime_config import ensure_default_configs

        async with AsyncSessionLocal() as db:
            added = await ensure_default_configs(db)
        if added:
            logger.info("配置中心种子：新增 %d 项默认检索配置", added)
    except Exception as exc:
        logger.warning("配置中心种子失败：%s", exc)
    # 深度推理（ReAct）配置种子：与检索配置同款语义（幂等、不覆盖已改值）。
    # 默认全开；**轮数/时长默认不限**（2026-10-19 用户要求），
    # 收敛由 no_progress 护栏保证；档位只影响检索广度。
    try:
        from app.services.agent.react_config import ensure_react_configs

        async with AsyncSessionLocal() as db:
            react_added = await ensure_react_configs(db)
        if react_added:
            logger.info("深度推理配置种子：新增 %d 项默认配置", react_added)
    except Exception as exc:
        logger.warning("深度推理配置种子失败：%s", exc)
    # 依赖自检（存储 / Embedding / Rerank；数据库已在入口预检）：
    # 真实探测而非配置检查，失败记 WARNING 不阻断启动——相关功能降级
    # （如 rerank 失败 → 检索层回退 RRF），与运行时策略一致。
    check_results = await run_startup_checks()
    log_check_results(check_results)
    # 缓存到 app.state：GET /health 回报上次启动结果；GET /health/check 实时复检
    app.state.startup_checks = [r.to_dict() for r in check_results]
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
# 业务路由统一双模认证（用户 JWT / 第三方 X-API-Key，见 services/auth.get_caller）。
# auth 路由自身不挂全局依赖：login/register 为开放端点，me/客户端管理自行校验。
# 健康检查 / OpenAPI 文档保持开放（探活与联调用）。
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(retrieve.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(generate.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(spaces.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(chat.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(conversations.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(templates.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(config.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(export.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])
app.include_router(audit.router, prefix=API_PREFIX, dependencies=[Depends(get_caller)])


@app.get("/health", tags=["health"])
async def health():
    """健康检查（开放）：进程状态 + 数据库/存储连通性 + 上次启动自检结果。

    联调排障入口：``storage``/``db`` 为实时轻量探测（SELECT 1 / ping），
    ``checks`` 为启动时四项自检的缓存结果（含 embedding/rerank 真实调用）。
    """
    from app.services.startup_checks import check_database
    from app.services.storage import storage_info

    try:
        storage = storage_info()
    except Exception as exc:  # pragma: no cover - 存储配置错误不应压垮探活
        storage = {"provider": "unknown", "ok": False, "detail": {"error": str(exc)}}
    try:
        db = (await check_database()).to_dict()
    except Exception as exc:  # pragma: no cover
        db = {"name": "database", "ok": False, "latency_ms": 0, "detail": str(exc)}
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "storage": storage,
        "db": db,
        "checks": getattr(app.state, "startup_checks", []),
    }


@app.get("/health/check", tags=["health"])
async def health_check():
    """实时复检（开放）：对 数据库/存储/Embedding/Rerank 重新执行一次真实探测。

    运维排障一键入口：修改 .env 或重启依赖服务后，无需重启后端即可核验
    四项依赖的当前可用性。探测方式与启动自检、业务调用同源。
    """
    from app.services.startup_checks import log_check_results, run_startup_checks

    results = await run_startup_checks()
    log_check_results(results)
    app.state.startup_checks = [r.to_dict() for r in results]
    return {"all_ok": all(r.ok for r in results), "checks": [r.to_dict() for r in results]}
