"""企业公文知识库 · 平台侧 Mock 接口服务（供第三方公文系统联调）。

本服务严格实现《接口文档》（docs/企业公文知识库_接口文档.md）v1 标准契约，
目的是让第三方公文系统在**后端未完全实现**时即可并行联调：
  - 统一响应信封 { code, message, data, trace_id }
  - 文档接入 / 检索 / 生成 / 模板 / 导出 / 配置 全端点
  - 检索端点**模拟**密级 + 权限下推过滤（演示契约行为，非真实检索）
  - 内置种子公文数据（仿四川省政府公开版公文），开箱即用

运行（在 backend 目录下，复用已有虚拟环境）：
    uv run python mock_api/platform_mock.py
默认端口 8800；也可：uv run uvicorn mock_api.platform_mock:app --port 8800

注意：本 Mock 的字段形状以《接口文档》为准；后端真实实现后续将在 M5/M6 与之对齐。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(
    title="GovDoc-RAG Platform Mock",
    version="mock-1.0",
    description="第三方联调用 Mock；严格对齐《接口文档》v1 契约",
)

# =============================================================================
# 我方制定的标准：密级权重（数值越大密级越高）
# 第三方将自身分级映射到该四级枚举，检索以我方枚举判定上限。
# =============================================================================
SECURITY_WEIGHT: dict[str, int] = {
    "PUBLIC": 0,
    "INTERNAL": 1,
    "RESTRICTED": 2,
    "SECRET": 3,
}


# =============================================================================
# 统一响应信封
# =============================================================================
def envelope(code: int, message: str, data: Any) -> dict[str, Any]:
    """构造标准响应包。trace_id 用于全链路追踪。"""
    return {
        "code": code,
        "message": message,
        "data": data,
        "trace_id": uuid.uuid4().hex,
    }


# =============================================================================
# Pydantic 契约模型（对齐《接口文档》v1）
# =============================================================================
SecurityLevel = Literal["PUBLIC", "INTERNAL", "RESTRICTED", "SECRET"]


class DocMeta(BaseModel):
    """文档元数据（接入必带，我方标准字段）。"""

    external_id: str
    title: str
    doc_type: str
    issuer: str | None = None
    doc_no: str | None = None
    publish_date: str | None = None
    security_level: SecurityLevel = "INTERNAL"
    permission_tags: list[str] = Field(default_factory=list)
    owner_org: str | None = None
    source_system: str | None = None
    content_ref: str | None = None


class IngestBody(BaseModel):
    docs: list[DocMeta]


class Context(BaseModel):
    """调用上下文：由上游网关注入，我方强制执行过滤。"""

    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevel = "INTERNAL"
    sensitive_words: list[str] | None = None
    tenant: str | None = None


class RetrieveBody(BaseModel):
    query: str
    context: Context
    top_k: int = 10


class GenerateBody(BaseModel):
    task: str = "draft"
    doc_type: str | None = None
    query: str
    elements: dict[str, Any] = Field(default_factory=dict)
    context: Context


class ExportBody(BaseModel):
    content: str
    template_id: str | None = None
    format: str = "docx"


# =============================================================================
# 内存态存储（Mock 用，不做持久化）
# =============================================================================
documents_store: dict[str, dict[str, Any]] = {}
chunks_store: dict[str, dict[str, Any]] = {}   # chunk_id -> chunk
tasks_store: dict[str, dict[str, Any]] = {}    # task_id -> 状态
templates_store: dict[str, dict[str, Any]] = {}
config_store: dict[str, list[dict[str, Any]]] = {
    "sensitive_word": [
        {"category": "sensitive_word", "key": "敏感词-A", "value": {"words": ["绝密工程", "内部代号X"]}, "scope": None, "is_active": True}
    ],
    "format_adapter": [
        {"category": "format_adapter", "key": "oa-v2", "value": {"file_ext": "pdf", "layout": "A4-红头"}, "scope": None, "is_active": True}
    ],
    "model_profile": [
        {"category": "model_profile", "key": "default-local", "value": {"provider_type": "local", "model_name": "qwen3-32b-instruct", "dims": 1024}, "scope": None, "is_active": True}
    ],
}


# =============================================================================
# 种子公文数据（仿四川省政府公开版公文；覆盖不同文种/密级/权限，便于演示过滤）
# =============================================================================
def _seed() -> None:
    seed_docs = [
        {
            "external_id": "MOCK-OA-0001",
            "title": "关于做好汛期安全生产工作的通知",
            "doc_type": "通知",
            "issuer": "办公厅",
            "doc_no": "川办函〔2025〕12号",
            "publish_date": "2025-03-01",
            "security_level": "INTERNAL",
            "permission_tags": ["scope:dept:总部", "scope:role:公文员"],
            "owner_org": "办公厅",
            "source_system": "公文系统V2",
            "chunks": [
                "各单位要牢固树立安全发展理念，切实增强责任意识，落实汛期值班值守制度。",
                "各分公司应于本月末前完成一次全面隐患排查，建立台账，明确整改责任人与完成时限。",
                "严格执行领导带班制度，确保信息畅通，遇突发情况第一时间上报总部应急办。",
            ],
        },
        {
            "external_id": "MOCK-OA-0002",
            "title": "关于2024年度重点工作完成情况的报告",
            "doc_type": "报告",
            "issuer": "战略发展部",
            "doc_no": "川战报〔2025〕3号",
            "publish_date": "2025-01-15",
            "security_level": "RESTRICTED",
            "permission_tags": ["scope:dept:战略发展部", "scope:role:部门负责人"],
            "owner_org": "战略发展部",
            "source_system": "公文系统V2",
            "chunks": [
                "2024年度营业收入同比增长8.2%，利润总额完成年度目标的102%。",
                "涉及未公开并购意向的内容仅限战略发展部与高管范围知悉，严禁外传。",
            ],
        },
        {
            "external_id": "MOCK-OA-0003",
            "title": "关于商请支持算力中心建设的函",
            "doc_type": "函",
            "issuer": "信息中心",
            "doc_no": "川信函〔2025〕5号",
            "publish_date": "2025-02-20",
            "security_level": "PUBLIC",
            "permission_tags": [],
            "owner_org": "信息中心",
            "source_system": "公文系统V2",
            "chunks": [
                "为推进数字化转型，拟于年内建成企业级算力中心，恳请上级在用电指标上予以支持。",
            ],
        },
    ]
    for d in seed_docs:
        doc_id = uuid.uuid4().hex
        documents_store[doc_id] = {k: v for k, v in d.items() if k != "chunks"}
        documents_store[doc_id]["id"] = doc_id
        documents_store[doc_id]["chunk_count"] = len(d["chunks"])
        for i, text in enumerate(d["chunks"], start=1):
            cid = uuid.uuid4().hex
            chunks_store[cid] = {
                "chunk_id": cid,
                "doc_id": doc_id,
                "doc_no": d["doc_no"],
                "title": d["title"],
                "snippet": text,
                "security_level": d["security_level"],
                "permission_tags": d["permission_tags"],
                "chunk_seq": i,
                "score": round(0.95 - i * 0.03, 3),
            }


_seed()


# =============================================================================
# 过滤逻辑（与 app.core.security.build_visibility_filter 语义一致，用于演示）
# =============================================================================
def _visible(chunk: dict[str, Any], ctx: Context) -> bool:
    """判断切片对调用方是否可见（语义严格对齐 app.core.security.build_visibility_filter）。

    规则：
      1. 密级上限：切片密级权重必须 ≤ 调用方 max_clearance 权重。
      2. 权限下推：
         - 切片无任何权限标签（公开/无约束）→ 对所有调用方可见（含有 scope 的调用方）。
         - 切片带权限标签且有 caller_scope → 标签交集非空才可见。
         - 切片带权限标签但无 caller_scope（无 scope 的上游）→ 不可见。
    该语义经 smoke_remote.py 在真实库上验证；Mock 与后端必须保持一致，避免联调时行为打架。
    """
    if SECURITY_WEIGHT.get(chunk["security_level"], 0) > SECURITY_WEIGHT.get(ctx.max_clearance, 0):
        return False
    tags = chunk.get("permission_tags") or []
    tags_empty = len(tags) == 0
    if ctx.caller_scope:
        # 公开/无约束切片对所有 scope 调用方可见；带标签切片需 scope 交集
        return tags_empty or bool(set(tags) & set(ctx.caller_scope))
    # 无 scope 的上游：仅允许无任何权限约束（公开）的切片
    return tags_empty


# =============================================================================
# 健康检查
# =============================================================================
@app.get("/health")
async def health() -> dict[str, Any]:
    return envelope(0, "ok", {"status": "ok", "version": app.version, "environment": "mock"})


# =============================================================================
# 文档接入
# =============================================================================
@app.post("/v1/documents")
async def ingest_docs(body: IngestBody) -> dict[str, Any]:
    accepted, failed, task_ids = 0, 0, []
    for meta in body.docs:
        if meta.external_id in {d["external_id"] for d in documents_store.values()}:
            failed += 1
            continue
        task_id = "t-" + uuid.uuid4().hex[:12]
        tasks_store[task_id] = {
            "task_id": task_id,
            "status": "COMPLETED",
            "progress": 100,
            "doc_count": 1,
            "message": "解析完成（Mock 不真实切片/向量化）",
        }
        # 直接落库（Mock 省略真实解析/切片/向量化）
        doc_id = uuid.uuid4().hex
        documents_store[doc_id] = {**meta.model_dump(), "id": doc_id, "chunk_count": 1}
        task_ids.append(task_id)
        accepted += 1
    return envelope(0, "ok", {"accepted": accepted, "failed": failed, "task_ids": task_ids})


@app.get("/v1/documents/tasks/{task_id}")
async def task_status(task_id: str = Path(...)) -> dict[str, Any]:
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return envelope(0, "ok", task)


# =============================================================================
# 检索（模拟混合检索 + 密级/权限下推过滤）
# =============================================================================
@app.post("/v1/retrieve")
async def retrieve(body: RetrieveBody) -> dict[str, Any]:
    ctx = body.context
    visible = [c for c in chunks_store.values() if _visible(c, ctx)]
    # 简单按预置 score 排序，取 top_k
    visible.sort(key=lambda c: c["score"], reverse=True)
    top = visible[: body.top_k]
    truncated = len(chunks_store) > len(visible)
    citations = [
        {
            "chunk_id": c["chunk_id"],
            "doc_id": c["doc_id"],
            "doc_no": c["doc_no"],
            "title": c["title"],
            "snippet": c["snippet"],
            "score": c["score"],
            "citation": {"doc_no": c["doc_no"], "title": c["title"], "chunk_seq": c["chunk_seq"]},
        }
        for c in top
    ]
    return envelope(
        0,
        "ok",
        {
            "citations": citations,
            "truncated_by_security": truncated,
            "note": "Mock 检索为演示过滤行为，非真实向量/关键词召回",
        },
    )


# =============================================================================
# 推理 / 问答（起草辅助）
# =============================================================================
@app.post("/v1/generate")
async def generate(body: GenerateBody) -> dict[str, Any]:
    ctx = body.context
    visible = [c for c in chunks_store.values() if _visible(c, ctx)]
    # 取前 2 条作为引用来源
    refs = visible[:2]
    answer = (
        f"【{body.doc_type or '公文'}草稿】\n根据检索到的{len(refs)}份参考资料，"
        f"就「{body.query}」拟写如下内容（此为 Mock 生成，仅用于联调验证链路）：\n"
        "一、提高思想认识；二、明确责任分工；三、强化督导检查。[1][2]"
    )
    citations = [
        {
            "marker": f"[{i + 1}]",
            "doc_no": c["doc_no"],
            "title": c["title"],
            "chunk_id": c["chunk_id"],
            "snippet": c["snippet"],
        }
        for i, c in enumerate(refs)
    ]
    # 敏感词命中演示（命中即 mask）
    hits: list[dict[str, Any]] = []
    words = ctx.sensitive_words or ["绝密工程", "内部代号X"]
    for w in words:
        if w in answer:
            hits.append({"word": w, "action": "masked", "position": [answer.find(w), answer.find(w) + len(w)]})
    return envelope(
        0,
        "ok",
        {
            "answer": answer,
            "citations": citations,
            "sensitive_hits": hits,
            "hallucination_flag": False,
        },
    )


# =============================================================================
# 模板库（我方维护，支持第三方导入）
# =============================================================================
@app.get("/v1/templates")
async def list_templates() -> dict[str, Any]:
    return envelope(0, "ok", list(templates_store.values()))


@app.post("/v1/templates")
async def create_template(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
    doc_type = item.get("doc_type", "未知")
    templates_store[doc_type] = {
        "doc_type": doc_type,
        "name": item.get("name", doc_type),
        "layout": item.get("layout", {}),
        "is_active": True,
    }
    return envelope(0, "ok", templates_store[doc_type])


@app.post("/v1/templates/import")
async def import_template(item: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return envelope(0, "ok", {"imported": True, "preview": item})


# =============================================================================
# 导出下载（我方提供；此处生成真实 docx 便于联调）
# =============================================================================
@app.post("/v1/export")
async def export_doc(body: ExportBody) -> dict[str, Any]:
    file_id = uuid.uuid4().hex
    return envelope(
        0,
        "ok",
        {"download_url": f"/v1/export/{file_id}", "file_id": file_id, "format": body.format},
    )


@app.get("/v1/export/{file_id}")
async def download_export(file_id: str = Path(...)) -> FileResponse:
    # 生成一份最小 docx（python-docx 已在后端依赖中），便于第三方验证下载链路
    from docx import Document

    doc = Document()
    doc.add_heading("公文导出样例（Mock）", level=1)
    doc.add_paragraph(f"文件ID：{file_id}")
    doc.add_paragraph("本文件由 Mock 服务生成，仅用于联调验证导出下载链路。")
    fd, path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    doc.save(path)
    return FileResponse(
        path,
        filename=f"{file_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# =============================================================================
# 配置中心
# =============================================================================
@app.get("/v1/config/{category}")
async def list_config(category: str = Path(...)) -> dict[str, Any]:
    return envelope(0, "ok", config_store.get(category, []))


@app.put("/v1/config/{category}")
async def upsert_config(category: str = Path(...), item: dict[str, Any] = Body(...)) -> dict[str, Any]:
    config_store.setdefault(category, [])
    key = item.get("key")
    for existing in config_store[category]:
        if existing["key"] == key:
            existing.update(item)
            return envelope(0, "ok", existing)
    config_store[category].append({**item, "category": category})
    return envelope(0, "ok", item)


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info")
