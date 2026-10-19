"""导出下载路由：/v1/export。

导出 = 导出【已生成并存库】的内容：按 conversation_id + message_id 精确定位
某一轮起草的 assistant 消息正文，直接渲染 docx，**不重新检索/生成**。

安全：导出前对正文二次脱敏（敏感词表可能生成后更新）+ 所引文档密级复核，
不因内容已落库而放松出口管控。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import security_weight
from app.core.security import redact_sensitive
from app.db.models import ConfigKV, Conversation, ConversationMessage, Document, DraftReference, Template
from app.db.session import get_db
from app.schemas.contracts import ExportDocxRequest
from app.services.auth import Caller, get_caller

router = APIRouter(prefix="/export", tags=["export"])


async def _active_sensitive_words(db: AsyncSession) -> list[str]:
    """解析当前生效的敏感词表（配置中心 category=sensitive_word 且 is_active）。"""
    rows = (
        await db.execute(
            select(ConfigKV.key).where(
                ConfigKV.category == "sensitive_word",
                ConfigKV.is_active.is_(True),
            )
        )
    ).scalars().all()
    return [w for w in rows if w]


async def _load_usable_template(
    db: AsyncSession, template_code: str | None, caller: Caller
) -> Template | None:
    """加载导出版式模板并做所有权校验（他人模板不可用）。

    返回模板行（供读取 layout 版式：字体/红头等）；未指定模板返回 None
    （走公文标准默认版式）。校验失败直接 403。
    """
    if not template_code:
        return None
    row = (
        await db.execute(select(Template).where(Template.code == template_code))
    ).scalar_one_or_none()
    if row and not (
        row.owner_type == "system"
        or (
            row.owner_type == ("user" if caller.kind == "user" else "client")
            and row.owner_id == caller.id
        )
    ):
        raise HTTPException(status_code=403, detail="无权使用该模板（仅创建者与系统标准模板可用）")
    return row


def _clearance_allowed(clearance: str, caller_max: str) -> bool:
    """引用文档密级是否不超过调用方额度（兼容历史代码/中文名）。"""
    return security_weight(clearance) <= security_weight(caller_max)


@router.post("/docx")
async def export_docx(
    req: ExportDocxRequest, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """导出已生成内容（按会话+消息定位，不重新生成）为 docx。

    权限：第三方需具备 chat 能力域。安全：导出前对正文二次脱敏 + 所引文档
    密级复核，不因内容已落库而放松。
    """
    if not caller.allows_scope("chat"):
        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")
    template = await _load_usable_template(db, req.template_code, caller)
    # 上传参考文档版式（2026-10-19）：reference_id 优先于模板——
    # 参考文档本来就是"这一稿的格式来源"（用户上传时选择了按格式生成）
    reference = await _load_reference_layout(db, req.reference_id, caller)
    return await _run_export(req, caller, db, template, reference_layout=reference)


async def _load_reference_layout(
    db: AsyncSession, reference_id: str | None, caller: Caller
) -> dict | None:
    """读取上传参考文档的版式画像（供导出渲染覆盖字体/红头）。

    仅上传者本人可引用（404 防探测）；画像无版式（PDF/OFD/TXT）时返回
    None——导出保持 GB/T 9704 默认版式（与生成时提示词口径一致）。
    """
    if not reference_id:
        return None
    row = (
        await db.execute(
            select(DraftReference).where(DraftReference.id == reference_id)
        )
    ).scalar_one_or_none()
    if row is None or not (
        row.owner_type == ("user" if caller.kind == "user" else "client")
        and row.owner_id == caller.id
    ):
        raise HTTPException(status_code=404, detail="参考文档不存在或无权使用")
    layout = row.profile.get("layout") if isinstance(row.profile, dict) else None
    return layout or None


async def _run_export(
    req: ExportDocxRequest, caller: Caller, db: AsyncSession, template: Template | None = None,
    reference_layout: dict | None = None,
) -> Response:
    # 1) 定位会话
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == req.conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 2) 定位目标 assistant 消息（版本）：指定优先，否则取最新一条
    if req.message_id:
        target = (
            await db.execute(
                select(ConversationMessage).where(
                    ConversationMessage.id == req.message_id,
                    ConversationMessage.conversation_id == req.conversation_id,
                )
            )
        ).scalar_one_or_none()
        if not target or target.role != "assistant":
            raise HTTPException(status_code=400, detail="指定的消息不存在或非起草正文")
    else:
        target = (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == req.conversation_id,
                    ConversationMessage.role == "assistant",
                )
                .order_by(ConversationMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if not target or not (target.content or "").strip():
        raise HTTPException(status_code=400, detail="该会话尚无可导出的起草内容")

    # 3) 安全复核：二次脱敏 + 密级复核（防御纵深）
    words = await _active_sensitive_words(db)
    content, hits = redact_sensitive(target.content or "", words)
    doc_ids = [
        c.get("document_id") for c in (target.citations or []) if isinstance(c, dict) and c.get("document_id")
    ]
    if doc_ids:
        docs = (
            await db.execute(select(Document.id, Document.security_level).where(Document.id.in_(doc_ids)))
        ).all()
        for doc_id, sec in docs:
            if not _clearance_allowed(sec, caller.max_clearance):
                raise HTTPException(
                    status_code=403, detail="导出内容含超出您密级额度的引用，已拒绝导出"
                )

    # 4) 渲染 docx（公文版式：标题层级/字体/缩进行距/表格/落款/页码）
    #    模板 layout 可覆盖正文字体与红头样式；缺省走 GB/T 9704 标准版式。
    #    上传参考文档（reference_id）的 layout 优先——它就是这一稿的
    #    格式来源（与生成链路提示词口径一致，2026-10-19）。
    from app.services.docx_render import render_docx

    layout = reference_layout or (template.layout if template else None)
    doc_blob = render_docx(
        title=conv.title or "公文起草",
        content=content,
        citations=[c for c in (target.citations or []) if isinstance(c, dict)],
        sensitive_hits=hits,
        layout=layout,
    )

    # 直接返回内存字节流（避免临时文件落盘与并发句柄问题）
    safe_name = (conv.title or "公文")[:20] + ".docx"
    blob = doc_blob
    # RFC 6266：filename 仅允许 ASCII，中文名放到 filename*（UTF-8 编码），
    # 避免 Starlette 头编码（Latin-1）失败导致 500
    disp = f"attachment; filename=\"document.docx\"; filename*=UTF-8''{quote(safe_name)}"
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": disp},
    )
