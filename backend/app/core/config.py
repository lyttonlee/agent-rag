"""应用配置中心。

所有运行时参数均来自环境变量（.env 或容器环境变量），符合 12-factor。
配置项与《接口文档》《详细功能设计》的"配置中心"章节对齐：
- 数据库连接（PostgreSQL + pgvector）
- 对象存储（存储抽象层：local 文件系统默认 / s3 兼容可选）
- 模型 Provider（local / cloud / std）及其端点
- 涉密不出网：ENVIRONMENT=production 时禁止 cloud Provider（启动自检）
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 定位：固定为本文件的上两级目录（backend/.env），与进程 CWD 解耦。
# 历史教训：此前用相对路径 ".env"，若从项目根等其他目录执行
# `uvicorn app.main:app`，.env 加载不到，全部配置静默回退默认值——
# PG 指向 127.0.0.1:5432（无监听）→ 表现为"数据库连接不上"。
# 环境变量优先级仍高于 .env（12-factor 不变），容器部署只注环境变量即可。
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# 我方制定的标准密级枚举（第三方映射自身分级到此枚举）
# 2026-10-15 起由四级扩为六级（用户需求）：非密 < 内部 < 普通商密 < 核心商密 < 密级 < 机密。
# 设计要点：商密两级采用非整数权重（2.3/2.7），落在"内部"与"密级"之间，
# 体现"商业秘密密级高于内部资料、低于国家秘密"的分层定位；
# 权重只用于服务端可见性比较（<=），不对外暴露为业务字段。
SecurityLevel = Literal[
    "PUBLIC", "INTERNAL", "COMMERCIAL", "CORE_COMMERCIAL", "CLASSIFIED", "TOP_SECRET"
]
# 密级权重：数值越大密级越高（不可随意调值——存量数据的可见性关系依赖此全序）
SECURITY_WEIGHT: dict[str, float] = {
    "PUBLIC": 1.0,            # 非密
    "INTERNAL": 2.0,          # 内部
    "COMMERCIAL": 2.3,        # 普通商密
    "CORE_COMMERCIAL": 2.7,   # 核心商密
    "CLASSIFIED": 3.0,        # 密级
    "TOP_SECRET": 4.0,        # 机密
}
# 中文展示名（前端下拉 / 统计图表 / 文档回显共用，单一事实源）
SECURITY_LEVEL_LABELS: dict[str, str] = {
    "PUBLIC": "非密",
    "INTERNAL": "内部",
    "COMMERCIAL": "普通商密",
    "CORE_COMMERCIAL": "核心商密",
    "CLASSIFIED": "密级",
    "TOP_SECRET": "机密",
}
# 兼容别名 → 规范枚举值（两类来源）：
#   1) 历史四级代码（2026-10-15 前的存量数据与旧客户端仍在用）：
#      RESTRICTED（受限，权重2）→ COMMERCIAL（2.3）；SECRET（秘密，权重3）→ TOP_SECRET（4）。
#      序位一致 ⇒ 迁移前后可见性关系完全不变（见迁移 0015）。
#   2) 第三方按中文习惯直传（"机密"/"商密"…）：归一后落库，避免 422 拒收。
_LEGACY_LEVEL_ALIASES: dict[str, str] = {
    "RESTRICTED": "COMMERCIAL",
    "SECRET": "TOP_SECRET",
    "非密": "PUBLIC",
    "公开": "PUBLIC",
    "内部": "INTERNAL",
    "普通商密": "COMMERCIAL",
    "商密": "COMMERCIAL",
    "核心商密": "CORE_COMMERCIAL",
    "密级": "CLASSIFIED",
    "机密": "TOP_SECRET",
    "秘密": "TOP_SECRET",
}


def normalize_security_level(value: str | None, default: str = "INTERNAL") -> str:
    """把任意密级输入归一为规范枚举值。

    接受：规范代码（PUBLIC/…）、历史代码（RESTRICTED/SECRET）、中文名
    （机密/商密/…）、大小写变体。无法识别时回落 default——调用方负责
    传入安全缺省（文档密级缺省 INTERNAL，额度缺省 PUBLIC 由 security_weight 保证）。
    """
    if not value:
        return default
    v = str(value).strip()
    if v in SECURITY_WEIGHT:
        return v
    upper = v.upper()
    if upper in SECURITY_WEIGHT:
        return upper
    return _LEGACY_LEVEL_ALIASES.get(v, _LEGACY_LEVEL_ALIASES.get(upper, default))


def security_weight(level: str | None) -> float:
    """密级权重（统一入口，兼容历史代码/中文名）。

    无法识别时按最低权重（PUBLIC）处理——fail-closed：认不出的密级
    一律当作最公开处理只会让结果更少，绝不会越权放行。
    """
    return SECURITY_WEIGHT.get(
        normalize_security_level(level, default="PUBLIC"), SECURITY_WEIGHT["PUBLIC"]
    )


class Settings(BaseSettings):
    """全局配置。读取顺序：环境变量 > .env > 默认值。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "GovDoc-RAG"
    app_version: str = "1.2.0"
    environment: Literal["dev", "test", "production"] = "dev"
    api_prefix: str = "/v1"
    log_level: str = "INFO"

    # ---- PostgreSQL + pgvector ----
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "rag"
    pg_password: str = "rag123456"
    pg_database: str = "gov_doc_rag"
    pg_pool_size: int = 10
    pg_max_overflow: int = 20

    # ---- 对象存储（抽象层：local 本地文件系统 / s3 兼容 SeaweedFS 等）----
    # 默认 local：零镜像、零协议风险，适合本项目 20 万文档 / ~44GB 规模。
    # s3 模式可指向 SeaweedFS（Apache 2.0）/ Ceph RGW / 国产私有化对象存储；
    # 为兼容「按厂商命名」的直觉，STORAGE_PROVIDER 同时接受 "s3" 与 "SeaweedFS"
    # （二者等价，均走 S3Storage 后端，唯一区别是语义标签）。
    storage_provider: Literal["local", "s3", "SeaweedFS"] = "local"
    storage_root: str = "./data/gov-doc"               # local 模式根目录
    # s3 模式（可选）
    storage_s3_endpoint: str = "127.0.0.1:8333"
    storage_s3_bucket: str = "gov-doc"
    storage_s3_access_key: str = "admin"
    storage_s3_secret_key: str = "admin123"
    storage_s3_region: str = "us-east-1"
    storage_s3_secure: bool = False
    # 寻址风格：内网 IP+端口端点必须 path（endpoint/bucket/key）；
    # 仅当 SeaweedFS 前置域名且配了泛 DNS 时才考虑 virtual。
    storage_s3_addressing_style: Literal["path", "virtual"] = "path"

    # ---- Redis（可选）----
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---- 模型 Provider 默认栈（local = 我方 Qwen3 栈）----
    # 生成模型
    model_gen_base_url: str = "http://127.0.0.1:8000/v1"   # vLLM OpenAI 兼容端点
    model_gen_api_key: str = "EMPTY"
    model_gen_name: str = "qwen3-32b-instruct"
    # 向量化（离线一次性）
    model_emb_base_url: str = "http://127.0.0.1:8001/v1"
    model_emb_api_key: str = "EMPTY"
    model_emb_name: str = "qwen3-embedding-4b"
    embedding_dim: int = 1024
    # 重排（在线）
    model_rerank_base_url: str = "http://127.0.0.1:8002/v1"
    model_rerank_api_key: str = "EMPTY"
    model_rerank_name: str = "qwen3-reranker-0.6b"
    rerank_top_n: int = 50
    # 送 rerank 的单文档最大字符数（2026-10-13 实测标定）：
    # 内网 Qwen3-Reranker-0.6B 最大上下文 4096 tokens，存量长块（整篇 1 chunk，
    # 实测最长 5760 字）超限会被服务端 HTTP 400 拒绝，且长文打分耗时劣化严重。
    # 公文相关性信息集中在标题与开头，截断到 1200 字可把 8 文档批次从 >40s
    # 压到 ~6.5s。部署方若使用更大上下文的 reranker（如 32k），可调大本值。
    rerank_doc_max_chars: int = 1200

    # ---- 涉密不出网：生产环境拒绝云端 Provider ----
    # 部署方若使用 std（自有算力中心），其 base_url 必须为内网地址
    forbid_cloud_in_production: bool = True

    # ---- 检索参数 ----
    retrieve_keyword_top_n: int = 50
    retrieve_vector_top_n: int = 50
    retrieve_final_top_n: int = 8

    # ---- 认证（用户 2026-10-13 需求：用户认证 + 第三方注册调用认证）----
    # JWT 签名密钥：生产必须通过环境变量覆盖（默认值仅用于开发）
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    # 访问令牌有效期（小时）
    jwt_expire_hours: int = 12
    # 启动时种子的管理员账号（dev 开箱即用；生产应改密或禁用）
    admin_username: str = "admin"
    admin_password: str = "admin123"

    @property
    def database_url(self) -> str:
        """异步 SQLAlchemy 连接串。"""
        return (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def database_url_sync(self) -> str:
        """同步连接串（Alembic 迁移使用）。"""
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )


@lru_cache
def get_settings() -> Settings:
    """返回进程内单例配置。"""
    return Settings()
