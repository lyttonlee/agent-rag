"""数据模型：文档、切片、模板、配置 KV、文档空间、会话。

设计要点：
- 切片（chunk）**冗余存储 security_level 与 permission_tags**，
  以便检索阶段在切片粒度直接下推过滤（避免每次 JOIN 文档表）。
- 切片同样**冗余 space_id**（文档空间），支撑"按空间检索"的切片粒度下推。
- embedding 列使用 pgvector 的 Vector(embedding_dim) 类型。
- search_vec 为 tsvector 列（由 zhparser 中文分词生成），支撑 BM25 关键词召回。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import Base, TimestampMixin

_EMB_DIM = get_settings().embedding_dim


def _uuid() -> str:
    return uuid.uuid4().hex


class DocumentSpace(Base, TimestampMixin):
    """文档空间：知识库的逻辑分区（如"综合政务"/"安全生产"/"人事档案"）。

    文档上传/接入时归属到某一空间；检索/问答可按 space_ids（支持多个）
    限定知识范围。第三方系统通过同一套 /v1/spaces 接口创建空间并
    将文档接入指定空间，实现多租户式的知识隔离。
    """

    __tablename__ = "document_spaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    # 空间编码：第三方系统对接用的稳定标识（唯一，建议英文/数字）
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(512))
    # 默认空间：存量文档与未显式指定空间的接入文档归属；不可删除
    is_default: Mapped[bool] = mapped_column(default=False, index=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="space")


class Document(Base, TimestampMixin):
    """公文文档主表（第三方接入，携带我方标准元数据）。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)   # 文种
    issuer: Mapped[str | None] = mapped_column(String(256))
    doc_no: Mapped[str | None] = mapped_column(String(128), index=True)  # 文号
    publish_date: Mapped[datetime | None] = mapped_column(DateTime)
    security_level: Mapped[str] = mapped_column(String(16), index=True, default="INTERNAL")
    # 权限标签数组：scope:type:value
    permission_tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    owner_org: Mapped[str | None] = mapped_column(String(256))
    source_system: Mapped[str | None] = mapped_column(String(128))
    content_ref: Mapped[str | None] = mapped_column(String(512))   # 存储对象键（local 相对路径 / s3 对象键）
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 所属文档空间（删除空间时置空，文档不随空间删除而丢失）
    space_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_spaces.id", ondelete="SET NULL"), index=True
    )

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document")
    space: Mapped["DocumentSpace | None"] = relationship(back_populates="documents")


class Chunk(Base, TimestampMixin):
    """文档切片表（检索主表）。"""

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    # 上下文前缀：标题｜发文机关｜日期｜标题路径，提升中文公文检索质量
    content_prefix: Mapped[str | None] = mapped_column(String(512))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMB_DIM))
    # 冗余密级 / 权限，支撑检索下推过滤
    security_level: Mapped[str] = mapped_column(String(16), index=True, default="INTERNAL")
    permission_tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # 冗余文档空间：按空间检索时在切片粒度直接 WHERE 下推（免 JOIN documents）
    space_id: Mapped[str | None] = mapped_column(String(32), index=True)
    search_vec: Mapped[object] = mapped_column(TSVECTOR)            # 中文 BM25 向量
    tokens: Mapped[int] = mapped_column(Integer, default=0)         # 字数，用于计费/统计

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        # HNSW 向量索引（检索性能核心）
        Index(
            "ix_chunks_embedding_hnsw",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # tsvector GIN 索引（关键词召回）
        Index("ix_chunks_search_vec", search_vec, postgresql_using="gin"),
    )


class Template(Base, TimestampMixin):
    """文种模板库（我方维护，第三方可导入样式）。"""

    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    doc_type: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 版式 / 字体 / 落款等结构化模板（JSON 描述，渲染导出时使用）
    layout: Mapped[dict] = mapped_column(JSON, default=dict)
    content_ref: Mapped[str | None] = mapped_column(String(512))   # 模板样例文件
    is_active: Mapped[bool] = mapped_column(default=True)


class ConfigKV(Base, TimestampMixin):
    """配置中心 KV（敏感词、格式适配器、模型 Profile 等）。"""

    __tablename__ = "config_kv"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # category ∈ {sensitive_word, format_adapter, model_profile, dict_custom}
    category: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    scope: Mapped[str | None] = mapped_column(String(128))         # 可选：按部署方隔离
    is_active: Mapped[bool] = mapped_column(default=True)


# 入库任务状态机：PENDING → PARSING → CHUNKING → EMBEDDING → INDEXED
#                                              ↘ FAILED（任意阶段异常）
INGESTION_STATUSES = (
    "PENDING",
    "PARSING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXED",
    "FAILED",
)


class IngestionJob(Base, TimestampMixin):
    """入库异步任务（状态机 + 进度），供前端展示"文档入库进度"。

    一次文件/正文接入对应一个 job；后端以 asyncio 后台任务推进各阶段，
    并实时回写 status / progress / processed_chunks，前端轮询即可呈现步进式进度。
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(256))            # 原始文件名 / 标识
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", index=True
    )
    progress: Mapped[int] = mapped_column(Integer, default=0)      # 0-100
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    processed_chunks: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(String(32), index=True)

    # 接入参数快照（便于回溯与复现）
    strategy: Mapped[str] = mapped_column(String(16), default="paragraph")
    chunk_size: Mapped[int] = mapped_column(Integer, default=800)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=80)
    security_level: Mapped[str] = mapped_column(String(16), default="INTERNAL")
    doc_type: Mapped[str] = mapped_column(String(64), default="通知")


class AuditLog(Base, TimestampMixin):
    """审计日志：记录"谁 / 何时 / 做了什么 / 结果如何 / 主题 / 客体"。

    设计（满足《需求规格说明书》审计要求）：
    - actor / actor_ip：操作者身份与上手 IP（信任上游网关下发的 X-Operator，缺省 anonymous）。
    - action：语义动作（如"文档入库""检索""生成""配置变更"），method+endpoint 留痕。
    - subject_type / subject_id：操作主体（操作者维度，便于按人追溯）。
    - object_type / object_id：被操作客体（文档 / 任务 / 配置项等，便于按资源追溯）。
    - result / status_code / detail：成功或失败、HTTP 状态、事件摘要。
    - duration_ms：请求耗时，辅助性能审计。
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(128), index=True, default="anonymous")  # 操作者
    actor_ip: Mapped[str | None] = mapped_column(String(64))                          # 上手 IP
    action: Mapped[str] = mapped_column(String(64), index=True)                       # 语义动作
    method: Mapped[str | None] = mapped_column(String(16))                            # HTTP 方法
    endpoint: Mapped[str | None] = mapped_column(String(256))                         # 路由
    subject_type: Mapped[str | None] = mapped_column(String(32), index=True)          # 主题类型
    subject_id: Mapped[str | None] = mapped_column(String(128), index=True)           # 主题 id
    object_type: Mapped[str | None] = mapped_column(String(32), index=True)           # 客体类型
    object_id: Mapped[str | None] = mapped_column(String(128), index=True)            # 客体 id
    result: Mapped[str] = mapped_column(String(16), default="success")                # success/failure
    status_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Conversation(Base, TimestampMixin):
    """会话：智能问答 / 公文起草的多轮对话容器。

    一次会话包含多条消息（user/assistant 交替），支持：
    - 智能问答（conv_type=chat）：基于知识文档回答问题，可多轮追问；
    - 公文起草（conv_type=draft）：按文种模板生成草稿，可多轮修改。
    会话可绑定知识空间（space_ids），限定检索范围。
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(256), default="新会话")
    conv_type: Mapped[str] = mapped_column(String(16), default="chat", index=True)  # chat/draft
    # 会话绑定的知识空间（多选）；空列表 = 不限空间（受密级/权限约束）
    space_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class ConversationMessage(Base, TimestampMixin):
    """会话消息：一问一答为一条 user + 一条 assistant。

    assistant 消息携带 citations（引用溯源）与 sensitive_hits（敏感词命中），
    历史回看时仍可核对依据；停止生成时保存的部分答案同样留痕。
    """

    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), index=True)      # user / assistant
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    sensitive_hits: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    model: Mapped[str | None] = mapped_column(String(64))

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
