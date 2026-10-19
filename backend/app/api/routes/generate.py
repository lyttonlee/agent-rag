"""推理生成路由：/v1/generate（非流式，保留）与 /v1/generate/stream（SSE 流式）。

认证（2026-10-13）：双模认证（用户 JWT / 第三方 X-API-Key）；
生成链路内部透传请求体 max_clearance 给混合检索，因此必须在路由层
做与 /v1/retrieve、/v1/chat/stream 一致的两道管控——
1. 能力域：第三方客户端需具备 chat 域（users 不限制）；
2. 密级额度：请求声明的 max_clearance 被调用方额度压低（防超额检索涉密内容）。

上传参考文档（2026-10-19）：POST /v1/generate/reference 接收一份文件
（格式模板 / 仿写范文），落存储 + 解析画像（版式/结构/行文），返回
reference_id 供 GenerateRequest.reference_id 引用。参考文档**不进**
检索语料（与知识库文档解耦），仅上传者本人可引用。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DraftReference
from app.db.session import get_db
from app.schemas.contracts import (
    DraftReferenceResp,
    GenerateRequest,
    GenerateResp,
)
from app.services.audit import actor_from_request
from app.services.auth import Caller, get_caller
from app.services.generation import (
    assert_template_usable,
    generate_answer,
    stream_generate,
)
from app.services.reference_profile import extract_reference_profile
from app.services.storage import get_storage

router = APIRouter(prefix="/generate", tags=["generate"])


def _owner_of(caller: Caller) -> tuple[str, str, str]:
    """调用方 → (owner_type, owner_id, owner_name)（与 spaces 路由同口径）。

    用户 JWT → user/<user.id>；第三方 API Key → client/<client_id>。
    """
    kind = "user" if caller.kind == "user" else "client"
    return kind, caller.id, caller.name or caller.id

#: 参考文档大小上限（20MB）：参考文档只取版式/结构/摘录，不需要全量入库，
#: 与入库上传（无硬限但走切分）不同，这里直接拦住异常大文件。
_REFERENCE_MAX_BYTES = 20 * 1024 * 1024
#: 支持的参考文档扩展名（与入库链路一致，嗅探兜底无扩展名文件）
_REFERENCE_EXTS = (".docx", ".doc", ".pdf", ".ofd", ".txt", ".md")


async def _guard_caller(
    caller: Caller, req: GenerateRequest, db: AsyncSession
) -> None:
    """生成链路权限管控（与 retrieve/chat 一致）：能力域 + 密级额度压低
    + 模板所有权校验（用户 2026-10-14 需求：他人模板不可引用）。

    说明：
    1. generate_answer / stream_generate 会把 req.max_clearance 直接
       透传给 hybrid_retrieve，若不在此处压低，低额度客户端即可通过请求体
       声明高密级越权检索——这是密级过滤下推链路的入口，必须收口；
    2. SSE 流式响应头一旦发出就无法再改状态码，因此模板所有权校验
       必须提前到路由层完成（assert_template_usable）。
    """
    if not caller.allows_scope("chat"):
        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")
    req.max_clearance = caller.cap_clearance(req.max_clearance)
    await assert_template_usable(db, req, caller)


@router.post("", response_model=GenerateResp)
async def generate(
    req: GenerateRequest, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> GenerateResp:
    """检索增强生成，强制引用溯源 + 敏感词后处理（非流式）。"""
    await _guard_caller(caller, req, db)
    result = await generate_answer(db, req, caller=caller)
    return GenerateResp(**result)


@router.post("/stream")
async def generate_stream(
    req: GenerateRequest, request: Request, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式生成（SSE）：先回传检索元数据，再逐 token 返回正文，结尾脱敏。

    Agent 模式（默认开）下 meta/done 事件携带 intent 与 disclaimer
    （无材料兜底的甄别声明标志）。前端用 fetch + ReadableStream 消费，
    实时渲染，避免大草稿被判定超时。
    """
    await _guard_caller(caller, req, db)
    return StreamingResponse(
        stream_generate(db, req, actor=actor_from_request(request), caller=caller),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )


@router.post("/reference", response_model=DraftReferenceResp)
async def upload_reference(
    file: UploadFile = File(...),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> DraftReferenceResp:
    """上传起草参考文档（格式模板 / 仿写范文，2026-10-19）。

    同步解析画像并返回 reference_id；后续在 GenerateRequest.reference_id
    里回传即可「按该文档模板格式生成」或「仿写该文档」。

    - 与知识库文档（/documents/upload）**解耦**：不切分/不向量化/不检索，
      避免一次性参考文件污染语料；
    - 所有权：仅上传者本人（user）或其第三方客户端（client）可引用；
    - 大小上限 20MB；格式按扩展名 + magic bytes 双重判定。
    """
    if not caller.allows_scope("chat"):
        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"参考文档过大（{len(raw) / 1024 / 1024:.1f}MB），上限 20MB",
        )
    filename = file.filename or "参考文档"
    lower = filename.lower()
    if not lower.endswith(_REFERENCE_EXTS):
        # 无扩展名/陌生扩展名：按文件头嗅探兜底（与入库链路同口径）
        from app.services.parser import _sniff_format

        if not _sniff_format(raw):
            raise HTTPException(
                status_code=400,
                detail="不支持的参考文档格式（支持 DOCX/PDF/OFD/TXT/MD）",
            )

    # 画像解析（同步）：失败直接 400，不留"传了却用不上"的中间态
    try:
        fmt, profile = extract_reference_profile(filename, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    owner_type, owner_id, owner_name = _owner_of(caller)
    ref_id = uuid.uuid4().hex
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else fmt
    key = f"references/{ref_id}.{ext}"
    get_storage().save(key, raw)

    row = DraftReference(
        id=ref_id,
        filename=filename,
        format=fmt,
        size_bytes=len(raw),
        storage_key=key,
        profile=profile,
        status="PARSED",
        owner_type=owner_type,
        owner_id=owner_id,
        owner_name=owner_name,
    )
    db.add(row)
    await db.commit()
    return DraftReferenceResp(
        reference_id=ref_id,
        filename=filename,
        format=fmt,
        size_bytes=len(raw),
        status="PARSED",
        mode_default="format" if profile.get("has_real_layout") else "imitate",
        profile={
            "style_notes": profile.get("style_notes", ""),
            "outline": (profile.get("outline") or [])[:12],
            "para_count": profile.get("para_count", 0),
            "approx_words": profile.get("approx_words", 0),
            "has_real_layout": bool(profile.get("has_real_layout")),
        },
    )


@router.get("/reference/{reference_id}", response_model=DraftReferenceResp)
async def get_reference(
    reference_id: str,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> DraftReferenceResp:
    """查询参考文档画像（仅上传者本人）：多轮起草时回显/确认用。"""
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
    return DraftReferenceResp(
        reference_id=row.id,
        filename=row.filename,
        format=row.format,
        size_bytes=row.size_bytes,
        status=row.status,
        mode_default="format" if row.profile.get("has_real_layout") else "imitate",
        error=row.error,
        profile={
            "style_notes": row.profile.get("style_notes", ""),
            "outline": (row.profile.get("outline") or [])[:12],
            "para_count": row.profile.get("para_count", 0),
            "approx_words": row.profile.get("approx_words", 0),
            "has_real_layout": bool(row.profile.get("has_real_layout")),
        },
    )
