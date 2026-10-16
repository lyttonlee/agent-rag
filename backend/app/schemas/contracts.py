"""API 契约（请求 / 响应 DTO）。

字段与枚举严格对齐《接口文档》，作为我方制定标准的一部分。
第三方接入时按此契约传参；我方返回结构化响应（含 citations / sensitive_hits）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.core.config import normalize_security_level

# ---- 通用 ----
# 我方标准密级枚举（六级，2026-10-15 起）：权重见 app.core.config.SECURITY_WEIGHT
SecurityLevel = Literal[
    "PUBLIC", "INTERNAL", "COMMERCIAL", "CORE_COMMERCIAL", "CLASSIFIED", "TOP_SECRET"
]
# 密级入参类型：接受规范代码 / 历史四级代码（RESTRICTED/SECRET）/ 中文名
# （机密/商密…），统一归一为规范枚举——枚举扩容不破坏存量接入方（不会 422）
SecurityLevelInput = Annotated[str, AfterValidator(normalize_security_level)]
# 切分策略（与 chunker.ChunkStrategy 对应）
ChunkStrategy = Literal["paragraph", "fixed", "sentence", "section"]


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
    security_level: SecurityLevelInput = Field(
        "INTERNAL", description="密级：非密/内部/普通商密/核心商密/密级/机密（见接口文档 2.2）"
    )
    permission_tags: list[str] = Field(default_factory=list, description="scope:type:value")
    owner_org: str | None = None
    source_system: str | None = None
    space_id: str | None = Field(None, description="所属文档空间 ID；缺省归入默认空间")
    content: str | None = Field(None, description="内联正文；或与 content_ref 二选一")
    content_ref: str | None = Field(None, description="存储对象键：local 模式为相对路径，s3 模式为对象键；或与内联 content 文本二选一")
    # ---- 切分策略（RAG 精髓：可选切分方式）----
    chunk_strategy: ChunkStrategy = Field("paragraph", description="切分策略：paragraph 按段落 / fixed 定长 / sentence 按句 / section 按章节")
    chunk_size: int = Field(800, description="单切片最大字符数", ge=64, le=4000)
    chunk_overlap: int = Field(80, description="相邻切片重叠字符数", ge=0, le=1000)


class IngestResp(BaseModel):
    external_id: str
    document_id: str
    status: str
    chunk_count: int = 0
    message: str = ""


# ---- 入库任务（状态机 + 进度）----
class IngestionJobResp(BaseModel):
    """入库异步任务的实时状态（前端轮询展示进度）。"""
    id: str
    filename: str
    status: str                                  # PENDING/PARSING/CHUNKING/EMBEDDING/INDEXED/FAILED
    progress: int = 0                             # 0-100 百分比
    total_chunks: int = 0
    processed_chunks: int = 0
    error_message: str | None = None
    document_id: str | None = None
    strategy: str = "paragraph"
    chunk_size: int = 800
    chunk_overlap: int = 80
    security_level: str = "INTERNAL"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---- 文档列表 / 详情（管理视图，真实数据）----
class ChunkInfo(BaseModel):
    seq: int
    content: str
    security_level: str
    tokens: int = 0


class DocumentSummary(BaseModel):
    id: str
    external_id: str
    title: str
    doc_type: str
    issuer: str | None = None
    doc_no: str | None = None
    security_level: str
    permission_tags: list[str] = Field(default_factory=list)
    space_id: str | None = None
    # 所属空间名称（冗余展示，避免前端反查空间列表）
    space_name: str | None = None
    status: str
    chunk_count: int = 0
    created_at: datetime | None = None


class DocumentDetail(BaseModel):
    id: str
    external_id: str
    title: str
    doc_type: str
    issuer: str | None = None
    doc_no: str | None = None
    publish_date: datetime | None = None
    security_level: str
    permission_tags: list[str] = Field(default_factory=list)
    space_id: str | None = None
    space_name: str | None = None
    owner_org: str | None = None
    source_system: str | None = None
    status: str
    chunk_count: int = 0
    created_at: datetime | None = None
    chunks: list[ChunkInfo] = Field(default_factory=list)


class DocStats(BaseModel):
    total_documents: int = 0
    total_chunks: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_security: dict[str, int] = Field(default_factory=dict)


# ---- 文档空间 ----
class SpaceCreate(BaseModel):
    """创建文档空间（运维台与第三方系统共用）。

    parent_id：父空间 ID（层级结构，类似文件夹）；缺省/None = 根级空间。
    所有权：owner 由服务端按调用方身份写入（用户 JWT / 第三方 API Key），
    不接受客户端指定——空间仅创建者可见、可上传、可检索。
    """

    name: str = Field(..., min_length=1, max_length=128)
    code: str = Field(..., min_length=1, max_length=64, description="空间编码（唯一，第三方对接稳定标识）")
    description: str | None = Field(None, max_length=512)
    parent_id: str | None = Field(None, description="父空间 ID；缺省为根级空间")


class SpaceUpdate(BaseModel):
    """更新文档空间（名称/描述/父空间；code 与默认标记不可改）。

    parent_id 支持把空间移动到其他父级下；服务端会阻止把空间移动到
    自己的子孙节点下（防环）。仅 owner 可更新；默认空间仅 admin。
    """

    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)
    parent_id: str | None = Field(None, description="父空间 ID；null 表示移到根级")


class SpaceResp(BaseModel):
    """文档空间响应（含实时文档数统计、层级与所有权视图）。

    所有权字段（2026-10-14）：
    - owner_type：system=公共（默认空间）/ user / client；
    - owner_id / owner_name：创建者标识与展示名；
    - is_mine：当前调用方是否为创建者（前端据此开放编辑/删除操作）；
    - can_upload：当前调用方是否可向该空间上传文档。
    """

    id: str
    name: str
    code: str
    description: str | None = None
    is_default: bool = False
    parent_id: str | None = None
    owner_type: str = "user"
    owner_id: str | None = None
    owner_name: str | None = None
    is_mine: bool = False
    can_upload: bool = False
    doc_count: int = 0
    chunk_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---- 检索 ----
class RetrieveRequest(BaseModel):
    query: str
    doc_types: list[str] | None = None
    space_ids: list[str] | None = Field(None, description="限定检索的文档空间（多选）；缺省不限空间")
    top_n: int | None = 8
    # 调用方上下文（上游网关下发，我方强制执行过滤）
    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevelInput = "INTERNAL"
    rerank: bool = True
    debug: bool = Field(
        False, description="调试：返回检索过程 trace（各阶段召回/融合/rerank 原始分/耗时），供排查检索质量"
    )


class Citation(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    doc_no: str | None = None
    snippet: str
    score: float
    # 完整正文：仅条件父块注入的大块携带（生成提示词优先用它而非 200 字
    # snippet，否则注入失去意义）；小块为 None，行为与旧版一致。
    content: str | None = None
    # 切片粒度：small=检索命中单元；large=父块注入单元（P1 长短切分）
    granularity: str = "small"


class RetrieveResp(BaseModel):
    query: str
    citations: list[Citation]
    total_matched: int
    truncated_by_security: bool = False   # 是否存在因密级/权限被过滤的结果
    top_vector_distance: float | None = None  # 向量路 top1 余弦距离（"无有用信息"判定信号）
    keyword_hits: int = 0                 # 关键词路命中数（"无有用信息"判定信号）
    trace: dict | None = None             # 检索过程调试信息（debug=True 时返回，见 RetrieveRequest.debug）


# ---- 推理生成 ----
class GenerateRequest(BaseModel):
    query: str
    doc_types: list[str] | None = None
    space_ids: list[str] | None = Field(None, description="限定检索的文档空间（多选）；缺省不限空间")
    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevelInput = "INTERNAL"
    template_doc_type: str | None = None  # 按文种模板起草（文种分类）
    template_code: str | None = None  # 具体模板编码（同一文种可配置多模板，精确选择某一类）
    conversation_id: str | None = Field(None, description="所属会话（公文起草多轮修改）；缺省自动创建")
    sensitive_words: list[str] | None = Field(
        None, description="请求级敏感词覆盖；缺省用配置中心词库"
    )
    rerank: bool = Field(
        False, description="生成链路是否启用重排；起草场景默认关闭（重排端点慢时避免久等，检索问答默认开启）"
    )
    agent_mode: bool | None = Field(
        None, description="Agent 模式：意图路由 + 分步执行；缺省走配置中心开关（默认开启），false 强制回退纯直答"
    )
    stream: bool = False


class GenerateResp(BaseModel):
    answer: str
    citations: list[Citation]
    sensitive_hits: list[str] = Field(default_factory=list)
    model: str
    truncated_by_security: bool = False


# ---- 导出（基于已生成内容，不重新生成）----
class ExportDocxRequest(BaseModel):
    """导出【已生成】内容：按会话 + 消息精确定位某一轮起草正文，直接渲染 docx。

    与 GenerateRequest 解耦：导出不需要 query/rerank/space_ids 等生成参数，
    也不调用模型——内容来自会话内已落库的 assistant 消息（天然版本管理）。
    """

    conversation_id: str = Field(..., description="起草会话 ID（必填，定位会话）")
    message_id: str | None = Field(
        None,
        description="指定导出的 assistant 消息版本；缺省取会话内最新一条 assistant 消息",
    )
    template_code: str | None = Field(None, description="版式模板 code（可选，仅影响 docx 排版）")


# ---- 配置中心 ----
class ConfigItem(BaseModel):
    category: str
    key: str
    value: dict[str, Any] = Field(default_factory=dict)
    scope: str | None = None
    is_active: bool = True


# ---- 会话（智能问答 / 公文起草共用）----
class ConversationCreate(BaseModel):
    conv_type: str = Field("chat", description="会话类型：chat 智能问答 / draft 公文起草")
    title: str | None = Field(None, max_length=256)
    space_ids: list[str] = Field(default_factory=list, description="绑定的知识空间（多选）")


class ConversationUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=256)
    space_ids: list[str] | None = None


class MessageAppend(BaseModel):
    """手动追加消息（如用户停止生成后保存部分答案）。"""

    role: str = Field("assistant", description="user / assistant")
    content: str
    citations: list[dict] = Field(default_factory=list)
    sensitive_hits: list[str] = Field(default_factory=list)
    model: str | None = None
    intent: str | None = Field(None, description="Agent 意图标识（assistant 消息留痕）")
    reasoning: str | None = Field(None, description="模型思考链全文（可折叠展开查看）")


class MessageResp(BaseModel):
    id: str
    role: str
    content: str
    citations: list[dict] = Field(default_factory=list)
    sensitive_hits: list[str] = Field(default_factory=list)
    model: str | None = None
    intent: str | None = None
    reasoning: str | None = None
    created_at: datetime | None = None


class ConversationResp(BaseModel):
    id: str
    title: str
    conv_type: str
    # 起草会话绑定的文种（draft 类型才有意义；历史还原时回显用）
    doc_type: str | None = None
    space_ids: list[str] = Field(default_factory=list)
    message_count: int = 0
    last_message_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 搜索命中片段（仅 keyword 搜索且命中消息内容时返回，供前端历史栏
    # 高亮展示"为什么这条会话被搜到"；普通列表与标题命中为 null）
    match_snippet: str | None = None


class ConversationDetail(ConversationResp):
    messages: list[MessageResp] = Field(default_factory=list)


# ---- 智能问答 ----
class ChatRequest(BaseModel):
    """智能问答请求（多轮：conversation_id 为空时自动创建会话）。"""

    conversation_id: str | None = None
    query: str
    space_ids: list[str] | None = Field(None, description="本次问答限定的知识空间；缺省沿用会话绑定空间")
    doc_types: list[str] | None = None
    caller_scope: list[str] = Field(default_factory=list)
    max_clearance: SecurityLevelInput = "INTERNAL"
    top_n: int | None = 8
    rerank: bool = Field(True, description="问答链路默认启用重排（提升引用质量）")
    agent_mode: bool | None = Field(
        None, description="Agent 模式：意图路由 + 分步执行；缺省走配置中心开关（默认开启），false 强制回退纯直答"
    )
    sensitive_words: list[str] | None = None


# ---- 审计日志 ----
class AuditLogResp(BaseModel):
    """审计日志条目：谁 / 何时 / 做了什么 / 结果 / 主题 / 客体。"""
    id: str
    actor: str                                  # 操作者（谁）
    actor_ip: str | None = None                  # 上手 IP
    action: str                                  # 语义动作（做了什么）
    method: str | None = None
    endpoint: str | None = None
    subject_type: str | None = None              # 主题类型（操作者维度）
    subject_id: str | None = None                # 主题 id
    object_type: str | None = None               # 客体类型（资源维度）
    object_id: str | None = None                 # 客体 id
    result: str = "success"                      # success / failure
    status_code: int | None = None
    detail: str = ""
    duration_ms: int | None = None
    created_at: datetime | None = None


# =========================================================================
# 认证（用户 2026-10-13 需求）
# =========================================================================

class LoginReq(BaseModel):
    """用户登录（运维台/控制台）。"""

    username: str
    password: str


class RegisterReq(BaseModel):
    """运维台用户自助注册（用户 2026-10-14 需求：登录/注册功能）。

    与第三方「注册调用」（/auth/clients/register 拿 API Key）是两套体系：
    本端点面向**自然人**——前端控制台的登录账号，注册成功直接下发 JWT
    （免去再登录一次），角色固定为最低权限的 viewer，禁用/提权由 admin
    在运维台侧操作（本期不做用户管理页，DB 侧维护）。
    """

    username: str = Field(..., min_length=2, max_length=64, description="登录用户名（唯一）")
    password: str = Field(..., min_length=6, max_length=128, description="口令（至少 6 位）")
    display_name: str = Field(default="", max_length=64, description="显示名（可选）")


class UserResp(BaseModel):
    """用户信息（不含口令哈希等敏感字段）。"""

    id: str
    username: str
    display_name: str = ""
    role: str
    enabled: bool = True
    last_login_at: datetime | None = None


class LoginResp(BaseModel):
    """登录成功响应：JWT + 用户信息。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="有效期（秒）")
    user: UserResp


class ClientRegisterReq(BaseModel):
    """第三方客户端注册（"注册调用"的注册环节）。

    提交后返回 client_id 与 api_key——**api_key 仅此一次明文返回**，
    请妥善保存；后续调用只需携带 X-API-Key 头。
    """

    name: str = Field(..., description="客户端名称（建议：接入系统名，便于审计）")
    scopes: list[str] = Field(
        default_factory=lambda: ["chat", "retrieve"],
        description='允许的能力域：chat/retrieve/documents/spaces/conversations/templates/config/audit/ops，或 ["*"] 全开',
    )
    max_clearance: SecurityLevelInput = Field(
        "INTERNAL",
        description="可检索最高密级额度：非密/内部/普通商密/核心商密/密级/机密（见接口文档 2.2）",
    )


class ClientResp(BaseModel):
    """第三方客户端信息（api_key 永不明文返回，仅展示前缀）。"""

    client_id: str
    name: str
    key_prefix: str = Field("", description="API Key 前缀（如 sk-ab12…），仅供核对")
    scopes: list[str] = Field(default_factory=list)
    max_clearance: str = "INTERNAL"
    enabled: bool = True
    expires_at: datetime | None = None
    created_by: str = ""
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class ClientRegisterResp(ClientResp):
    """注册响应：唯一一次返回 api_key 明文。"""

    api_key: str = Field(..., description="API Key 明文，仅此次返回，请立即保存")
