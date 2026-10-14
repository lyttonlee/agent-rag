"""清理所有权验证脚本（verify_ownership.py）产生的测试数据。

范围（仅删测试痕迹，不动真实业务数据）：
1. 模板：删除全部非 system 模板（当前库内均为验证脚本创建的测试模板），
   并把被旧代码误改过的系统标准模板「通知」恢复为种子版式；
2. 文档：删除 external_id LIKE 'own-test-%' 的验证文档（chunks 级联删除），
   及其入库任务；
3. 空间：删除 code LIKE 'own-a-%' / 'own-c1-%' 的验证空间。

用法：PYTHONPATH=. uv run python scripts/clean_ownership_testdata.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, select, update

from app.api.routes.templates import _SEED_TEMPLATES
from app.db.models import Chunk, Document, DocumentSpace, IngestionJob, Template
from app.db.session import AsyncSessionLocal


async def clean() -> None:
    async with AsyncSessionLocal() as db:
        # ---- 1. 模板 ----
        # 新代码创建的测试模板带 owner（user/client）；另需覆盖"旧代码误建"
        # 的残留：旧代码不写 owner_type，落库时继承 server_default='system'，
        # 表现为 code=tpl-a-{hex} 或「通知_{hex6}」（上传模板的自动编码）。
        rows = (
            await db.execute(
                select(Template).where(
                    (Template.owner_type != "system")
                    | Template.code.like("tpl-a-%")
                    | Template.code.op("~")("^通知_[0-9a-f]{6}$")
                )
            )
        ).scalars().all()
        for r in rows:
            await db.delete(r)
        tpl_deleted = len(rows)

        # 恢复系统标准模板「通知」为种子版式（防旧代码误改残留）
        seed = next(t for t in _SEED_TEMPLATES if t["doc_type"] == "通知")
        await db.execute(
            update(Template)
            .where(Template.code == "通知", Template.owner_type == "system")
            .values(name=seed["name"], layout=seed["layout"])
        )

        # ---- 2. 文档与入库任务 ----
        # 先显式删 chunks：ORM 关系的默认删除行为会把 chunks.document_id
        # 置空（NOT NULL 约束直接报错），而 DB 层 ondelete=CASCADE 又不会
        # 被 ORM 触发（relationship 未配 passive_deletes），故手动删。
        docs = (
            await db.execute(select(Document).where(Document.external_id.like("own-test-%")))
        ).scalars().all()
        doc_ids = [d.id for d in docs]
        if doc_ids:
            await db.execute(delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
            await db.execute(delete(IngestionJob).where(IngestionJob.document_id.in_(doc_ids)))
            for d in docs:
                await db.delete(d)
        jobs = (
            await db.execute(
                select(IngestionJob).where(
                    IngestionJob.filename.like("%own-test%")
                    | IngestionJob.filename.like("%越权上传%")
                    | IngestionJob.filename.like("%正常上传%")
                )
            )
        ).scalars().all()
        for j in jobs:
            await db.delete(j)

        # ---- 3. 验证空间 ----
        spaces = (
            await db.execute(
                select(DocumentSpace).where(
                    DocumentSpace.code.like("own-a-%") | DocumentSpace.code.like("own-c1-%")
                )
            )
        ).scalars().all()
        for s in spaces:
            await db.delete(s)

        await db.commit()
        print(
            f"清理完成：删除测试模板 {tpl_deleted} 个、验证文档 {len(docs)} 篇、"
            f"孤儿任务 {len(jobs)} 条、验证空间 {len(spaces)} 个；"
            f"系统模板「通知」已恢复种子版式"
        )


if __name__ == "__main__":
    asyncio.run(clean())
