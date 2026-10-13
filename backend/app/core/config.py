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
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# 我方制定的标准密级枚举（第三方映射自身分级到此枚举）
SecurityLevel = Literal["PUBLIC", "INTERNAL", "RESTRICTED", "SECRET"]
# 密级权重：数值越大密级越高
SECURITY_WEIGHT: dict[str, int] = {
    "PUBLIC": 0,
    "INTERNAL": 1,
    "RESTRICTED": 2,
    "SECRET": 3,
}


class Settings(BaseSettings):
    """全局配置。读取顺序：环境变量 > .env > 默认值。"""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    # s3 模式可指向 SeaweedFS（Apache 2.0）/ Ceph RGW / 国产私有化对象存储。
    storage_provider: Literal["local", "s3"] = "local"
    storage_root: str = "./data/gov-doc"               # local 模式根目录
    # s3 模式（可选）
    storage_s3_endpoint: str = "127.0.0.1:8333"
    storage_s3_bucket: str = "gov-doc"
    storage_s3_access_key: str = "admin"
    storage_s3_secret_key: str = "admin123"
    storage_s3_region: str = "us-east-1"
    storage_s3_secure: bool = False

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
