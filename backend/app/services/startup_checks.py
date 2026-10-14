"""启动自检：数据库 / 对象存储 / Embedding / Rerank 四项可用性探测。

设计目标（用户 2026-10-14 需求）：
1. 启动即对四项外部依赖做真实探测（不是配置存在性检查，而是连通 + 认证 +
   最小请求），失败时给出**可操作的排障提示**，而不是把问题留到第一次
   业务请求才以 500 暴露；
2. 数据库不可用 = 致命（服务无意义，启动失败并明确告知原因）；
   存储 / embedding / rerank 不可用 = 降级（记录 WARNING，服务仍起，
   相关功能在调用时再失败——与检索层 rerank 降级 RRF 的策略一致）；
3. 探测结果同时暴露给 ``GET /health``（上次启动结果）与
   ``GET /health/check``（实时复检），运维可随时一键核验。

探测方式与业务调用严格同源（同一 ModelProvider / storage 抽象），
避免"自检通过但业务调用失败"的两张皮。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# 单项探测超时（秒）：embedding/rerank 冷启动可能较慢，给足余量；
# 库内 SELECT 1 极快，3s 足够（超时大概率是网络/防火墙问题）。
_CHECK_TIMEOUT = 20.0


@dataclass
class CheckResult:
    """单项探测结果。detail 面向运维：成功给关键事实，失败给排障线索。"""

    name: str
    ok: bool
    latency_ms: int
    detail: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


def _db_failure_hints(exc: Exception) -> str:
    """数据库连接失败时的可操作提示（按错误类型给线索）。"""
    s = get_settings()
    base = (
        f"无法连接 PostgreSQL {s.pg_host}:{s.pg_port}/{s.pg_database}"
        f"（用户 {s.pg_user}）：{type(exc).__name__}: {exc}"
    )
    msg = str(exc).lower()
    if "password" in msg or "authentication" in msg:
        hint = "凭证被拒：检查 .env 的 PG_USER/PG_PASSWORD 与服务端一致"
    elif "does not exist" in msg and "database" in msg:
        hint = f"数据库不存在：需先在 {s.pg_host} 上创建库 {s.pg_database}"
    elif (
        "refused" in msg
        or "拒绝" in msg
        or "unreachable" in msg
        or "timeout" in msg
        or "timed out" in msg
        or "connect" in msg
    ):
        hint = "网络不可达/被拒：确认 PG 已启动、端口放通、pg_hba.conf 允许该来源 IP"
    elif "too many clients" in msg:
        hint = "连接数打满：存在僵死连接（pg_stat_activity 排查）或调大 max_connections"
    else:
        hint = "请确认 PG 服务状态、.env 中 PG_* 配置与网络连通性"
    return f"{base}。排障：{hint}"


async def check_database() -> CheckResult:
    """数据库探测：真实连接 + SELECT 1 + 版本/表数/alembic 版本。"""
    t0 = time.perf_counter()
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            version = (await db.execute(text("SELECT version()"))).scalar() or ""
            tables = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
                    )
                )
            ).scalar()
            alembic_ver = (
                await db.execute(
                    text(
                        "SELECT version_num FROM alembic_version LIMIT 1"
                    )
                )
            ).scalar()
        latency = int((time.perf_counter() - t0) * 1000)
        pg_ver = version.split("(")[0].strip() if version else "unknown"
        return CheckResult(
            name="database",
            ok=True,
            latency_ms=latency,
            detail=(
                f"PostgreSQL 可达（{pg_ver}），public 表 {tables} 张，"
                f"alembic 版本 {alembic_ver or '未初始化'}"
            ),
        )
    except Exception as exc:
        return CheckResult(
            name="database",
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            detail=_db_failure_hints(exc),
        )


async def check_storage() -> CheckResult:
    """对象存储探测：storage_info() 内部对 local 探根目录可写、s3 探 head_bucket。"""
    t0 = time.perf_counter()
    try:
        from app.services.storage import storage_info

        info = storage_info()
        latency = int((time.perf_counter() - t0) * 1000)
        if info["ok"]:
            return CheckResult(
                name="storage",
                ok=True,
                latency_ms=latency,
                detail=f"{info['provider']} 模式就绪：{info['detail']}",
            )
        return CheckResult(
            name="storage",
            ok=False,
            latency_ms=latency,
            detail=(
                f"{info['provider']} 模式不可用：{info['detail']}。"
                "排障：s3/SeaweedFS 模式确认端点/凭证/桶配置（.env STORAGE_S3_*）"
            ),
        )
    except Exception as exc:
        return CheckResult(
            name="storage",
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            detail=f"存储自检异常：{type(exc).__name__}: {exc}",
        )


async def check_embedding() -> CheckResult:
    """Embedding 探测：真实调用一次向量化，校验维度与配置一致。"""
    t0 = time.perf_counter()
    try:
        from app.services.model_provider import ModelProvider

        s = get_settings()
        provider = ModelProvider(role="emb")
        vecs = await asyncio.wait_for(
            provider.embed(["启动自检探针：网络安全管理"]), timeout=_CHECK_TIMEOUT
        )
        dim = len(vecs[0]) if vecs else 0
        latency = int((time.perf_counter() - t0) * 1000)
        if dim != s.embedding_dim:
            return CheckResult(
                name="embedding",
                ok=False,
                latency_ms=latency,
                detail=(
                    f"模型 {s.model_emb_name} 返回维度 {dim}，"
                    f"与配置 EMBEDDING_DIM={s.embedding_dim} 不一致"
                    "（更换模型后须同步改维度并重建向量索引）"
                ),
            )
        return CheckResult(
            name="embedding",
            ok=True,
            latency_ms=latency,
            detail=f"{s.model_emb_base_url} 模型 {s.model_emb_name} 可用，维度 {dim}",
        )
    except Exception as exc:
        s = get_settings()
        return CheckResult(
            name="embedding",
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            detail=(
                f"{s.model_emb_base_url}（模型 {s.model_emb_name}）调用失败："
                f"{type(exc).__name__}: {exc}。排障：确认 vLLM/推理服务已启动、"
                "模型名与服务端 /v1/models 列表一致"
            ),
        )


async def check_rerank() -> CheckResult:
    """Rerank 探测：真实调用一次打分，验证端点 + 模型名 + 响应结构。"""
    t0 = time.perf_counter()
    try:
        from app.services.model_provider import ModelProvider

        s = get_settings()
        provider = ModelProvider(role="rerank")
        scores = await asyncio.wait_for(
            provider.rerank(
                "网络安全边界防护要求",
                ["运维部门应关闭非必要对外端口，升级防火墙策略"],
                top_n=1,
            ),
            timeout=_CHECK_TIMEOUT,
        )
        latency = int((time.perf_counter() - t0) * 1000)
        return CheckResult(
            name="rerank",
            ok=True,
            latency_ms=latency,
            detail=(
                f"{s.model_rerank_base_url} 模型 {s.model_rerank_name} 可用，"
                f"探针得分 {scores[0]:.4f}" if scores else "响应为空"
            ),
        )
    except Exception as exc:
        s = get_settings()
        return CheckResult(
            name="rerank",
            ok=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            detail=(
                f"{s.model_rerank_base_url}（模型 {s.model_rerank_name}）调用失败："
                f"{type(exc).__name__}: {exc}。排障：确认服务已启动、模型名与"
                "服务端 /v1/models 一致（404 多为模型名不匹配）"
            ),
        )


async def run_startup_checks(include_models: bool = True) -> list[CheckResult]:
    """并发执行四项探测，返回结果列表（顺序固定：db → storage → emb → rerank）。

    Args:
        include_models: False 时只探数据库（供建表前的快速致命检查）。
    """
    checks = [check_database()]
    if include_models:
        checks += [check_storage(), check_embedding(), check_rerank()]
    return list(await asyncio.gather(*checks))


def log_check_results(results: list[CheckResult]) -> None:
    """把探测结果打成对齐的汇总日志（✓/✗ + 延迟 + 详情）。"""
    logger.info("===== 启动自检 =====")
    for r in results:
        mark = "✓" if r.ok else "✗"
        level = logging.INFO if r.ok else logging.WARNING
        logger.log(level, "  %s %-10s %5dms  %s", mark, r.name, r.latency_ms, r.detail)
    failed = [r.name for r in results if not r.ok]
    if failed:
        logger.warning("自检未通过项：%s（详见上方排障提示）", ", ".join(failed))
