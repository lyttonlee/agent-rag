"""一键清库：PG 业务表 + 对象存储（SeaweedFS/local）。

用途：冒烟测试/重跑验证前的数据重置。**只清业务数据，保留 alembic_version**
（迁移版本不能丢，否则启动时 upgrade head 会重跑全部迁移）。

清后需重启后端：启动时会自动重建种子数据——
    ensure_default_space（默认空间）/ ensure_seed_templates（标准模板库）/
    ensure_default_configs（检索配置，幂等不覆盖）

用法：
    python tools/reset_all_data.py            # 交互确认
    python tools/reset_all_data.py --yes      # 跳过确认（脚本化）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")  # .env 固定从 backend/ 加载

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402

# 全部业务表（顺序无关，CASCADE 处理外键）；alembic_version 故意不在列
BUSINESS_TABLES = [
    "chunks",
    "documents",
    "document_spaces",
    "templates",
    "config_kv",
    "ingestion_jobs",
    "audit_logs",
    "conversation",
    "conversation_messages",
    "users",
    "api_clients",
]


async def truncate_pg() -> list[str]:
    """TRUNCATE 全部业务表（RESTART IDENTITY 重置自增序列）。"""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename <> 'alembic_version'"
            )
        )
        existing = [r[0] for r in rows]
        targets = [t for t in existing if t in BUSINESS_TABLES] or existing
        # 表名来自 pg_tables 白名单交集，非用户输入，可安全拼接
        await conn.execute(text(f'TRUNCATE TABLE {", ".join(targets)} RESTART IDENTITY CASCADE'))
    return targets


def clear_storage() -> int:
    """清空对象存储全部对象（SeaweedFS S3 / LocalFS 均支持）。"""
    from app.services.storage import get_storage  # noqa: PLC0415

    st = get_storage()
    deleted = 0
    client = getattr(st, "client", None)
    if client is not None:  # S3 系：直接列举桶内对象批量删
        bucket = st.bucket
        resp = client.list_objects_v2(Bucket=bucket)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        for i in range(0, len(keys), 500):  # S3 单次删除上限 1000，取 500 保守
            client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 500]]}
            )
            deleted += len(keys[i : i + 500])
        return deleted
    # LocalFS：遍历根目录删文件（保留目录结构）
    root = Path(st.root) if hasattr(st, "root") else None
    if root and root.exists():
        for p in root.rglob("*"):
            if p.is_file():
                p.unlink()
                deleted += 1
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    s = get_settings()
    is_local = s.storage_provider.lower() == "local"
    print("将清空：")
    print("  - PostgreSQL：public 下全部业务表（保留 alembic_version）")
    if is_local:
        print(f"  - 对象存储：local  root={s.storage_root}")
    else:
        print(
            f"  - 对象存储：{s.storage_provider}  bucket={s.storage_s3_bucket} "
            f"endpoint={s.storage_s3_endpoint}"
        )
    if not args.yes:
        ans = input("确认执行？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消")
            return 1

    tables = asyncio.run(truncate_pg())
    print(f"[OK] PG 已清空 {len(tables)} 张表：{', '.join(sorted(tables))}")
    n = clear_storage()
    print(f"[OK] 对象存储已清空 {n} 个对象")
    print("\n下一步：重启后端（启动会自动重建默认空间/标准模板/检索配置种子）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
