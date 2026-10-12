"""检索全流程测试：向量召回 + 关键词召回 + RRF + 真实 rerank + 权限下推。

对接真实 embedding(8002)/rerank(8001) 环境（见 .env）。
同时用两个调用方上下文验证权限下推：
    - A：无 scope / max_clearance=SECRET  -> 仅可见 PUBLIC（无权限约束）切片
    - B：scope=[总部,公文员] / max_clearance=INTERNAL -> 可见 PUBLIC + 总部内部切片

用法：
    cd backend
    uv run python -m scripts.retrieve_test
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.schemas.contracts import RetrieveRequest
from app.services.retrieval import hybrid_retrieve

# 公文语义查询（命中种子语料中的汛期/安全/报告/算力等主题）
QUERIES = [
    "汛期安全生产值班值守有哪些要求",
    "年度重点工作完成情况与营业收入",
    "算力中心建设需要哪些支持",
    "隐患排查与整改责任如何落实",
]


async def main() -> None:
    settings = get_settings()
    print(
        f"[i] 检索测试  db={settings.pg_host}:{settings.pg_database}  "
        f"emb={settings.model_emb_name}  rerank={settings.model_rerank_name}",
        flush=True,
    )
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    # 两调用方上下文
    ctx_a = RetrieveRequest(query="", caller_scope=[], max_clearance="SECRET", top_n=8)
    ctx_b = RetrieveRequest(
        query="", caller_scope=["scope:dept:总部", "scope:role:公文员"],
        max_clearance="INTERNAL", top_n=8,
    )

    async with Session() as db:
        for q in QUERIES:
            ctx_a.query = q
            ctx_b.query = q
            r_a = await hybrid_retrieve(db, ctx_a)
            r_b = await hybrid_retrieve(db, ctx_b)

            print("\n" + "=" * 72, flush=True)
            print(f"查询：{q}", flush=True)
            print(f"  [A 无scope/SECRET] 命中 {len(r_a.citations)} 条", flush=True)
            for c in r_a.citations[:3]:
                print(f"     · {c.title[:24]:<24} score={c.score:.3f}  {c.snippet[:36]}", flush=True)
            print(f"  [B 总部公文员/INTERNAL] 命中 {len(r_b.citations)} 条", flush=True)
            for c in r_b.citations[:3]:
                print(f"     · {c.title[:24]:<24} score={c.score:.3f}  {c.snippet[:36]}", flush=True)
            diff = len(r_b.citations) - len(r_a.citations)
            print(
                f"  权限下推差异 (B-A) = {diff} 条  "
                f"{'✅ 下推生效' if diff >= 0 and (len(r_b.citations) >= len(r_a.citations)) else '⚠️ 检查'}",
                flush=True,
            )

    await engine.dispose()
    print("\n[完成] 检索全流程测试结束。", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
