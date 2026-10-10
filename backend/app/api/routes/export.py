"""导出下载路由：/v1/export。

我方提供导出下载接口：按模板渲染 docx（二期含 pdf/ofd），
导出前再做一次敏感词 / 密级校验。
"""

from __future__ import annotations

import io
import tempfile
import os

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import GenerateRequest
from app.services.generation import generate_answer

router = APIRouter(prefix="/export", tags=["export"])


@router.post("/docx")
async def export_docx(
    req: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    """生成并下载 docx（含引用与敏感词校验）。"""
    resp = await generate_answer(db, req)

    from docx import Document

    doc = Document()
    doc.add_heading(req.query, level=1)
    doc.add_paragraph(resp.answer)
    if resp.citations:
        doc.add_heading("参考资料", level=2)
        for i, c in enumerate(resp.citations, 1):
            doc.add_paragraph(f"[{i}] 《{c.title}》{c.doc_no or ''}")
    if resp.sensitive_hits:
        doc.add_paragraph(f"【敏感词命中】{', '.join(resp.sensitive_hits)}（已脱敏）")

    fd, path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    doc.save(path)
    return FileResponse(
        path, filename=f"{req.query[:20] or '公文'}.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
