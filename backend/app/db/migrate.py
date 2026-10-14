"""启动自动建表 / 更新：Alembic upgrade head（默认）+ create_all 兜底。

设计（用户 2026-10-14 需求："启动后端的时候数据库表应自动创建或更新"）：

1. **默认走 Alembic 迁移链**（而非 create_all）：
   create_all 只能"建缺失的表"，无法给已有表加列/改索引——这正是
   历史上 doc_type 唯一索引残留（0011 迁移才删掉）这类坑的根源。
   alembic upgrade head 语义严格覆盖"创建或更新"。

2. **PG 咨询锁防多进程竞争**：uvicorn 多 worker / 滚动重启时多个进程
   可能同时进 lifespan，用 ``pg_try_lock`` 保证同一时刻只有一个进程执行
   迁移；抢不到锁的进程等待锁超时后跳过（对方在迁，结果一致），
   不会无限阻塞启动（lock_timeout 15s）。

3. **dev/test 兜底 create_all**：迁移链异常（如手写迁移文件损坏）时，
   开发环境退化为直接建缺失表，保证"起得来"；生产环境不兜底——
   schema 漂移比启动失败更危险，必须显式修复迁移后再上线。

 Alembic 以编程方式执行（Config + command.upgrade），不依赖 shell CWD，
 与 .env 加载解耦（见 core.config 的同款处理）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# alembic.ini 定位：backend/alembic.ini（本文件的上两级 = backend/）
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
# 迁移专用咨询锁 ID（任意固定大整数，避开 PG 自带锁 ID 段）
_ADVISORY_LOCK_KEY = 727100003
# 抢锁等待上限：超过即认为其他进程正在迁移，跳过（不阻塞启动）
_LOCK_TIMEOUT_SECONDS = 15


def _run_alembic_upgrade() -> None:
    """同步执行 alembic upgrade head（在 worker 线程中调用，不阻塞事件循环）。"""
    settings = get_settings()
    cfg = Config(str(_ALEMBIC_INI))
    # env.py 自身也会用 settings 覆盖 URL，这里双保险（编程式调用时 ini 里的
    # 占位 url 不可靠）
    cfg.set_main_option("sqlalchemy.url", settings.database_url_sync)
    command.upgrade(cfg, "head")


def _psycopg_conninfo() -> str:
    """构造 psycopg 可用的连接串。

    注意：不能直接把 ``database_url_sync``（``postgresql+psycopg://``，
    含 SQLAlchemy 方言前缀）传给 psycopg——它只认 libpq conninfo 或
    ``postgresql://`` URI，否则报 ``missing "=" after ...``（实测踩坑）。
    这里剥掉方言前缀转为标准 URI。
    """
    url = get_settings().database_url_sync
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _try_advisory_lock() -> tuple[object, bool]:
    """尝试获取迁移咨询锁。返回 (连接, 是否持有)。

    实现说明：目标环境（192.168.6.49 的 PG）的 pg_proc 中**没有
    pg_try_lock**（函数级限制，pg_advisory_lock/unlock 均在），故不能用
    非阻塞 try_lock；改用阻塞 ``pg_advisory_lock`` + ``lock_timeout``
    （15s）实现同等语义：抢不到锁时超时抛 LockNotAvailable，调用方据此
    判定"其他进程正在迁移"并跳过，不会无限阻塞启动。
    """
    import psycopg

    conn = psycopg.connect(_psycopg_conninfo(), autocommit=True)
    # 注意：SET 语句不支持绑定参数（$1 报 syntax error），必须走 set_config()
    conn.execute(
        "SELECT set_config('lock_timeout', %s, false)",
        (f"{_LOCK_TIMEOUT_SECONDS}s",),
    )
    try:
        # pg_advisory_lock/pg_advisory_unlock 只接受 bigint，psycopg 推断的
        # integer 会报 UndefinedFunction，必须显式 ::bigint 转换。
        conn.execute("SELECT pg_advisory_lock(%s::bigint)", (_ADVISORY_LOCK_KEY,))
        return conn, True
    except psycopg.errors.LockNotAvailable:
        # 超时未抢到锁：其他进程正在迁移，关闭连接直接跳过
        conn.close()
        return None, False


def _upgrade_with_lock() -> str:
    """带咨询锁的迁移执行。返回状态说明（记日志用）。"""
    conn, got = _try_advisory_lock()
    try:
        if not got:
            return "skipped-locked"
        _run_alembic_upgrade()
        return "upgraded"
    finally:
        try:
            conn.execute(
                "SELECT pg_advisory_unlock(%s::bigint)", (_ADVISORY_LOCK_KEY,)
            )
        except Exception:  # noqa: BLE001 —— 锁随连接关闭必然释放，失败仅记
            pass
        conn.close()


async def upgrade_schema() -> str:
    """启动时把库结构升级到最新迁移版本。

    Returns:
        状态串："upgraded"（执行了迁移）/ "skipped-locked"（其他进程在迁）/
        "fallback-create-all"（dev 兜底）。
    """
    try:
        status = await asyncio.to_thread(_upgrade_with_lock)
        if status == "skipped-locked":
            logger.info("数据库结构迁移：其他进程正在执行，跳过")
        else:
            logger.info("数据库结构迁移：alembic upgrade head 完成")
        return status
    except Exception as exc:
        settings = get_settings()
        if settings.environment in ("dev", "test"):
            # dev 兜底：迁移链异常时退化为建缺失表，保证开发环境起得来
            logger.warning(
                "alembic 迁移失败（%s: %s），dev 环境兜底 create_all。"
                "注意：create_all 不会给已有表加列/改索引，请尽快修复迁移文件。",
                type(exc).__name__,
                exc,
            )
            from app.db.base import Base
            from app.db.session import engine

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return "fallback-create-all"
        # 生产不兜底：schema 漂移比启动失败更危险
        raise RuntimeError(
            f"数据库迁移失败（生产环境不兜底）：{type(exc).__name__}: {exc}。"
            "请修复 alembic 迁移文件后重启；如需紧急恢复，可临时设 "
            "ENVIRONMENT=dev 走 create_all 兜底（不推荐）。"
        ) from exc
