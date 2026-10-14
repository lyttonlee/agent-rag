"""清空环境数据：删除 PG 全部业务表数据 + SeaweedFS 桶全部对象。

⚠️ 破坏性操作（仅按用户明确指令执行）：
- PG：TRUNCATE 所有业务表（RESTART IDENTITY CASCADE），但**保留 alembic_version**
  （迁移版本追踪不能丢，否则后续 alembic 会误判重跑）。
- SeaweedFS：列出 gov-doc 桶全部对象并删除。
- 种子数据（admin / 默认空间 / 标准模板 / 默认配置）由后端启动时重新生成，此处不保留。

用法：uv run python scripts/reset_all_data.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal


async def reset_pg() -> list[str]:
    """清空所有业务表，返回被清空的表名清单。"""
    async with AsyncSessionLocal() as db:
        # 动态获取 public 下的所有表（排除 alembic_version 与系统表）
        tables = (
            await db.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename<>'alembic_version'"
                )
            )
        ).scalars().all()
        if not tables:
            print("[PG] 未找到业务表，跳过")
            return []
        # 单条 TRUNCATE 多表 + RESTART IDENTITY + CASCADE
        await db.execute(
            text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        )
        await db.commit()
        print(f"[PG] 已清空 {len(tables)} 张表：{', '.join(tables)}")
        return list(tables)


def reset_s3() -> int:
    """清空 SeaweedFS 桶全部对象，返回删除个数。"""
    import boto3
    from botocore.config import Config as BotoConfig

    s = get_settings()
    endpoint_url = ("https://" if s.storage_s3_secure else "http://") + s.storage_s3_endpoint
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=s.storage_s3_access_key,
        aws_secret_access_key=s.storage_s3_secret_key,
        region_name=s.storage_s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": s.storage_s3_addressing_style},
        ),
    )
    bucket = s.storage_s3_bucket
    deleted = 0
    token = None
    while True:
        kwargs = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        contents = resp.get("Contents", [])
        if contents:
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents], "Quiet": True},
            )
            deleted += len(contents)
            print(f"[S3] 已删除 {len(contents)} 个对象（累计 {deleted}）")
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    print(f"[S3] 桶 {bucket} 已清空，共删除 {deleted} 个对象")
    return deleted


async def main() -> int:
    print("=== 环境数据清空（PG 业务表 + SeaweedFS 桶）===")
    tables = await reset_pg()
    n = reset_s3()
    print(f"\n完成：PG 清空 {len(tables)} 张表，S3 删除 {n} 个对象。")
    print("提示：请重启后端以重新生成种子数据（admin/默认空间/标准模板/默认配置）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
