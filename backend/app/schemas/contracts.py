"""API 契约（请求 / 响应 DTO）。

字段与枚举严格对齐《接口文档》，作为我方制定标准的一部分。
第三方接入时按此契约传参；我方返回结构化响应（含 citations / sensitive_hits）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---- 通用 ----
SecurityLevel = Literal["PUBLIC", "INTERNAL", "RESTRICTED", "SECRET"]


class HealthResp(BaseModel):
    status: str = "ok"
    version: str
    environment: str


# ---- 文档接入 ----
class DocumentIngest(BaseModel):
    """第三方接入文档（携带我方标准元数据）。"""

    external_id: str = Field(..., description="第三方业务主键，幂等键")
    title: str
    doc_type: str = Field(..., description="文种：通知/报告/请示/函/通报/纪要/决定/意见")
    issuer: str | None = None
    doc_no: str | None = None
    publish_date: str | None = None            # ISO 日期
    security_level: SecurityLevel = "INTERNAL"
    permission_tags: list[str] = Field(default_factory=list, description="scope:type:value")
    owner_org: str | None = None
    source_system: str | None = None
    content: str | None = Field(None, description="内联正文；或与 content_ref 二选一")
    content_ref: str | None = Field(None, description="存储对象键：local 模式为相对路径，s3 模式为对象键；或与内联 content 文本二选一")


class IngestResp(BaseModel):
    external_id: str
    document_id: str
    status: str
    chunk_count: int = 0
    message: str = ""


# ---- 检索 ----
class RetrieveRequest(BaseModel):
    query: str
    doc_types: list[str] | None = None
    top_n: int | None = 8
    # 调用方上下文（上游网关下发，我方强制执行过滤）
    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevel = "INTERNAL"
    rerank: bool = True


class Citation(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    doc_no: str | None = None
    snippet: str
    score: float


class RetrieveResp(BaseModel):
    query: str
    citations: list[Citation]
    total_matched: int
    truncated_by_security: bool = False   # 是否存在因密级/权限被过滤的结果


# ---- 推理生成 ----
class GenerateRequest(BaseModel):
    query: str
    doc_types: list[str] | None = None
    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevel = "INTERNAL"
    template_doc_type: str | None = None  # 按文种模板起草
    sensitive_words: list[str] | None = Field(
        None, description="请求级敏感词覆盖；缺省用配置中心词库"
    )
    stream: bool = False


class GenerateResp(BaseModel):
    answer: str
    citations: list[Citation]
    sensitive_hits: list[str] = Field(default_factory=list)
    model: str
    truncated_by_security: bool = False


# ---- 配置中心 ----
class ConfigItem(BaseModel):
    category: str
    key: str
    value: dict[str, Any] = Field(default_factory=dict)
    scope: str | None = None
    is_active: bool = True
