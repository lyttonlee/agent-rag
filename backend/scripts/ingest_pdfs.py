"""将 sichuan_gov_docs/ 下全部 PDF 真实向量化入库。

对接真实 embedding 环境（见 .env 的 MODEL_EMB_BASE_URL，默认 192.168.6.49:8002）。
复用 app 的 ingest_document（解析 → 切片 → 向量化 → 落库 + 原文存储 + 中文 BM25）。

密级/权限混合标记（用于后续权限下推验证）：
    - 偶数索引文档 -> PUBLIC（无权限约束，对所有调用方可见）
    - 奇数索引文档 -> INTERNAL + ['scope:dept:总部','scope:role:公文员']

幂等：external_id 已存在则跳过，可安全重跑。

用法：
    cd backend
    uv run python -m scripts.ingest_pdfs --pdf-dir ../sichuan_gov_docs
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sys

# 允许以模块方式运行（python -m scripts.ingest_pdfs）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models import Document
from app.services.ingestion import ingest_document

DOC_TYPES = ["通知", "通报", "报告", "请示", "函", "纪要", "决定", "意见"]


def derive_meta(path: str, idx: int) -> dict:
    """从文件名推导元数据，并做密级/权限混合标记。"""
    fname = os.path.basename(path)
    stem = os.path.splitext(fname)[0]
    # 防御「双重扩展名」如 xxx.PDF.pdf：循环剥离尾部 .pdf（不区分大小写）
    while stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    doc_type = next((t for t in DOC_TYPES if t in stem), "公文")
    if idx % 2 == 0:
        security_level = "PUBLIC"
        permission_tags: list[str] = []
    else:
        security_level = "INTERNAL"
        permission_tags = ["scope:dept:总部", "scope:role:公文员"]
    return {
        "external_id": f"SEED-{stem}",
        "title": stem,
        "doc_type": doc_type,
        "issuer": "四川省政府（公开版样本）",
        "security_level": security_level,
        "permission_tags": permission_tags,
        "owner_org": "seed-corpus",
        "source_system": "sichuan_gov_docs",
        "filename": fname,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description="PDF 真实向量化入库")
    ap.add_argument("--pdf-dir", default="../sichuan_gov_docs")
    args = ap.parse_args()

    settings = get_settings()
    files = sorted(glob.glob(os.path.join(args.pdf_dir, "*.pdf")))
    if not files:
        print(f"[!] 未找到 PDF：{args.pdf_dir}", flush=True)
        return

    print(f"[i] 发现 {len(files)} 个 PDF，目标库={settings.pg_host}:{settings.pg_port}/{settings.pg_database}", flush=True)
    print(f"[i] embedding={settings.model_emb_base_url} model={settings.model_emb_name}", flush=True)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    ingested = skipped = failed = total_chunks = 0
    for idx, path in enumerate(files):
        raw = open(path, "rb").read()
        meta = derive_meta(path, idx)
        try:
            async with Session() as db:
                exists = (
                    await db.execute(
                        select(Document.id).where(Document.external_id == meta["external_id"])
                    )
                ).scalar_one_or_none()
                if exists:
                    print(f"[skip] {meta['title']}（已存在）", flush=True)
                    skipped += 1
                    continue
                res = await ingest_document(db, meta, raw)
                print(
                    f"[ok] {meta['title']}  sec={meta['security_level']:<8} "
                    f"tags={meta['permission_tags']}  chunks={res['chunk_count']}",
                    flush=True,
                )
                total_chunks += res["chunk_count"]
                ingested += 1
        except Exception as exc:  # 单篇失败不影响其余
            print(f"[FAIL] {meta['title']}: {type(exc).__name__}: {exc}", flush=True)
            failed += 1

    print(
        f"\n[完成] 新增 {ingested} 篇，跳过 {skipped} 篇，失败 {failed} 篇，累计切片 {total_chunks}",
        flush=True,
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
