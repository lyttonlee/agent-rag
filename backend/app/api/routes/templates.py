"""文种模板库路由：/v1/templates。

两条维护路径（对应运维台"模板库"页面）：
1. 上传模板文件（docx/pdf/txt）：解析正文 → 识别文种 → 提取章节结构 → 存库；
2. 结构化版式编辑：红头/字体/纸张/语气/章节（可视化表单，不接触原始 JSON）。

首次启动自动种子 8 类标准公文模板，保证模板库"开箱即用、非空白"。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template
from app.db.session import AsyncSessionLocal, get_db
from app.schemas.contracts import ConfigItem
from app.services.parser import parse_bytes
from app.services.storage import get_storage

router = APIRouter(prefix="/templates", tags=["templates"])

# 8 类标准公文模板种子（版式结构化，便于前端可视化编辑，而非裸 JSON）
_SEED_TEMPLATES: list[dict] = [
    {
        "doc_type": "通知",
        "name": "通知（通用）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 通知", "tone": "正式、明了",
            "sections": [
                {"key": "greeting", "label": "主送机关", "placeholder": "各有关单位：", "required": False},
                {"key": "body", "label": "正文", "placeholder": "写明事由、依据与执行要求", "required": True},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "通报",
        "name": "通报（情况/表彰/批评）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 通报", "tone": "客观、简洁",
            "sections": [
                {"key": "body", "label": "正文", "placeholder": "事实概述 → 分析/决定 → 要求", "required": True},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "报告",
        "name": "报告（工作/情况）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 关于××的报告", "tone": "陈述、严谨",
            "sections": [
                {"key": "greeting", "label": "主送机关", "placeholder": "上级机关：", "required": True},
                {"key": "body", "label": "正文", "placeholder": "情况 → 问题 → 下一步打算", "required": True},
                {"key": "end", "label": "结尾", "placeholder": "专此报告。", "required": False},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "请示",
        "name": "请示（事项审批）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 关于××的请示", "tone": "恳切、事由清晰",
            "sections": [
                {"key": "greeting", "label": "主送机关", "placeholder": "上级机关：", "required": True},
                {"key": "body", "label": "正文", "placeholder": "理由 → 事项 → 请求", "required": True},
                {"key": "end", "label": "结语", "placeholder": "妥否，请批示。", "required": False},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "函",
        "name": "函（商洽/询问/答复）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 函", "tone": "平实、客气",
            "sections": [
                {"key": "greeting", "label": "主送机关", "placeholder": "不相隶属机关：", "required": True},
                {"key": "body", "label": "正文", "placeholder": "商洽/询问/答复事项", "required": True},
                {"key": "end", "label": "结语", "placeholder": "特此函达，盼复。", "required": False},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "纪要",
        "name": "纪要（会议纪要）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "会议名称 + 纪要", "tone": "纪实、要点化",
            "sections": [
                {"key": "meta", "label": "会议要素", "placeholder": "时间 / 地点 / 主持人 / 参会", "required": True},
                {"key": "body", "label": "议定事项", "placeholder": "逐条列出决议与责任", "required": True},
                {"key": "sign", "label": "落款", "placeholder": "记录机关\\n日期", "required": False},
            ],
        },
    },
    {
        "doc_type": "决定",
        "name": "决定（重要事项）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 关于××的决定", "tone": "权威、决断",
            "sections": [
                {"key": "body", "label": "正文", "placeholder": "背景理由 → 决定事项 → 执行要求", "required": True},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
    {
        "doc_type": "意见",
        "name": "意见（指导/实施）",
        "layout": {
            "red_head": True, "font": "仿宋_GB2312", "paper": "A4",
            "subtitle": "发文机关 + 关于××的意见", "tone": "指导、可行",
            "sections": [
                {"key": "greeting", "label": "主送机关", "placeholder": "有关单位：", "required": False},
                {"key": "body", "label": "正文", "placeholder": "意义 → 措施 → 要求", "required": True},
                {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
            ],
        },
    },
]

# 标准文种集合（用于上传模板时识别文种）
_DOC_TYPES = ["通知", "通报", "报告", "请示", "函", "纪要", "决定", "意见"]

# 章节标题行：一、/ （一） / 1. / 1、 等公文常见编号
_HEADING_RE = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[\.、．])\s*(.{2,40})$")


def _guess_doc_type(filename: str, text: str) -> str:
    """识别模板文种：先看文件名，再看正文前 300 字。"""
    for t in _DOC_TYPES:
        if t in filename:
            return t
    head = (text or "")[:300]
    for t in _DOC_TYPES:
        if t in head:
            return t
    return "通知"


def _extract_layout(text: str) -> dict:
    """从模板正文启发式提取结构化版式（章节列表）。

    提取策略：扫描编号标题行（一、/（一）/1.）作为章节；提取不到时
    回退到通用「正文 + 落款」两段式，保证任何上传文件都能得到可用版式。
    """
    sections: list[dict] = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        label = m.group(1).strip()
        if not label or len(label) > 40:
            continue
        # 取标题行之后的第一段非空文本作为 placeholder 提示
        placeholder = ""
        for nxt in lines[i + 1 : i + 6]:
            if nxt.strip():
                placeholder = nxt.strip()[:60]
                break
        sections.append({
            "key": f"sec{len(sections) + 1}",
            "label": label,
            "placeholder": placeholder,
            "required": len(sections) == 0,
        })
        if len(sections) >= 8:
            break
    if not sections:
        sections = [
            {"key": "body", "label": "正文", "placeholder": "模板正文内容", "required": True},
            {"key": "sign", "label": "落款", "placeholder": "发文机关（印章）\\n日期", "required": True},
        ]
    return {
        "red_head": True,
        "font": "仿宋_GB2312",
        "paper": "A4",
        "subtitle": "",
        "tone": "",
        "sections": sections,
    }


async def ensure_seed_templates() -> int:
    """启动种子：若模板库为空，写入 8 类标准公文模板。返回新增数量。

    种子模板的 code 与 doc_type 同名（如「通知」），作为该文种的默认模板；
    后续可在「模板库」页为同一文种增配多个模板（code 不同）。
    """
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Template))).scalars().all()
        if existing:
            return 0
        for t in _SEED_TEMPLATES:
            db.add(Template(
                doc_type=t["doc_type"], code=t["doc_type"], name=t["name"],
                layout=t["layout"], is_active=True,
            ))
        await db.commit()
        return len(_SEED_TEMPLATES)


@router.post("/seed", summary="手动初始化标准模板库")
async def seed_templates() -> dict:
    added = await ensure_seed_templates()
    return {"added": added, "message": "模板库已就绪" if added else "模板库已存在，无需初始化"}


@router.post("/upload", summary="上传模板文件（docx/pdf/txt）")
async def upload_template(
    file: UploadFile = File(...),
    doc_type: str = Form(""),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> ConfigItem:
    """上传模板文件接入：解析正文 → 识别文种 → 提取章节结构 → 入库。

    原始文件存入对象存储（content_ref），前端可随时下载原件；
    解析出的结构化版式供可视化编辑与生成时按文种起草使用。
    """
    raw = await file.read()
    filename = file.filename or "template"
    # 解析放线程池：PDF/DOCX 解析为 CPU 密集型，避免阻塞事件循环
    parsed = await asyncio.to_thread(parse_bytes, filename, raw)
    text = parsed.text or ""

    resolved_type = doc_type or _guess_doc_type(filename, text)
    layout = _extract_layout(text)

    # 原始文件落库（对象存储）
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    key = f"templates/{uuid.uuid4().hex}.{ext}"
    get_storage().save(key, raw)

    # 每次上传作为**独立模板**（支持同一文种配置多个模板）：以 code 唯一标识，
    # 缺省用「文种_随机」生成，避免与同文种其它模板冲突；同名文种已有模板不再被覆盖。
    code = f"{resolved_type}_{uuid.uuid4().hex[:6]}"
    row = Template(
        doc_type=resolved_type,
        code=code,
        name=name or filename,
        layout=layout,
        content_ref=key,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ConfigItem(
        category="template",
        key=row.code,
        value={
            "doc_type": row.doc_type,
            "name": row.name,
            "layout": row.layout,
            "content_ref": row.content_ref,
        },
    )


@router.get("", summary="列出模板库")
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[ConfigItem]:
    """列出全部启用模板（同一文种可有多条，以 code 区分）。"""
    rows = (await db.execute(select(Template).where(Template.is_active.is_(True)).order_by(Template.doc_type, Template.created_at))).scalars().all()
    return [
        ConfigItem(
            category="template",
            key=t.code,
            value={
                "doc_type": t.doc_type,
                "name": t.name,
                "layout": t.layout,
                "content_ref": t.content_ref,
            },
        )
        for t in rows
    ]


@router.get("/{code}/file", summary="下载模板原始文件")
async def download_template(code: str, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """按模板 code 下载上传的模板原件（无上传文件时返回 404）。"""
    row = (
        await db.execute(select(Template).where(Template.code == code))
    ).scalar_one_or_none()
    if not row or not row.content_ref:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="该模板暂无上传的模板文件")
    data = get_storage().get(row.content_ref)
    ext = row.content_ref.rsplit(".", 1)[-1].lower()
    media = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "txt": "text/plain",
    }.get(ext, "application/octet-stream")
    # 说明：Content-Disposition 头按 latin-1 编码，中文文件名必须做 RFC 5987 百分号编码，
    # 否则（如"通知_template.txt"）会触发 UnicodeEncodeError 导致接口 500。
    # 同时保留纯 ASCII 的 filename= 兜底，兼容不支持 filename* 的老客户端。
    filename = f"{row.doc_type}_template.{ext}"
    return StreamingResponse(
        iter([data]),
        media_type=media,
        headers={
            "Content-Disposition": (
                f'attachment; filename="template.{ext}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.post("", summary="新增/更新模板（结构化版式）")
async def create_template(item: ConfigItem, db: AsyncSession = Depends(get_db)) -> ConfigItem:
    """按 code 保存结构化版式（红头/字体/纸张/章节），来自前端可视化编辑器。

    code 为模板唯一标识（同一文种可有多条，code 不同）；已存在则更新，否则新建。
    value 内需携带 doc_type（文种分类）与 name（模板名称）。
    """
    code = item.key
    if not code or not code.strip():
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="模板 code 不能为空")
    doc_type = (item.value.get("doc_type") or "").strip() or code
    name = item.value.get("name") or doc_type
    layout = item.value.get("layout", item.value)
    existing = (
        await db.execute(select(Template).where(Template.code == code))
    ).scalar_one_or_none()
    if existing:
        existing.doc_type = doc_type
        existing.name = name
        existing.layout = layout
    else:
        existing = Template(doc_type=doc_type, code=code, name=name, layout=layout)
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return ConfigItem(
        category="template",
        key=existing.code,
        value={
            "doc_type": existing.doc_type,
            "name": existing.name,
            "layout": existing.layout,
            "content_ref": existing.content_ref,
        },
    )


@router.delete("/{code}", summary="删除模板")
async def delete_template(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    existing = (
        await db.execute(select(Template).where(Template.code == code))
    ).scalar_one_or_none()
    if existing:
        await db.delete(existing)
        await db.commit()
        return {"deleted": True}
    return {"deleted": False}
