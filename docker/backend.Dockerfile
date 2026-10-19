# =============================================================================
# 企业公文知识库 · 后端服务镜像（仅项目镜像，不含基础组件）
#
# 构建（在仓库根目录执行）：
#   docker build -t gov-doc-rag-backend:1.2.0 -f docker/backend.Dockerfile backend/
#
# 产物内容：
#   - Python 3.12 运行时 + uv 同步锁版本依赖（uv.lock 锁死，保证可复现）
#   - app/（FastAPI 模块化单体）+ ofdkit/（OFD 解析独立库，workspace 成员）
#   - alembic/ + alembic.ini（启动时 lifespan 自动 upgrade head，无需手动迁移）
#   - 启动命令：uvicorn app.main:app
#
# 依赖的外部组件均由部署方既有环境提供（不在本镜像内）：
#   PostgreSQL(+pgvector/zhparser) / 模型服务(vLLM 等 OpenAI 兼容端点) /
#   Redis(可选) / 对象存储(默认 local 本地卷，可选 s3)
# =============================================================================
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="gov-doc-rag-backend" \
      org.opencontainers.image.description="企业公文知识库 RAG 引擎服务（解析+切片+检索+推理）" \
      org.opencontainers.image.version="1.2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    # uv 行为固化：不自动下载解释器（用基础镜像的 3.12）、字节码预编译加速首启
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # 应用默认配置（部署时用 -e / compose environment 覆盖）
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    LOG_LEVEL=INFO \
    STORAGE_PROVIDER=local \
    STORAGE_ROOT=/app/data/gov-doc

# ---------- 1. 系统依赖：curl（健康检查/排障） ----------
# 替换 Debian 源为清华源（与 docker/Dockerfile.pg 的国内加速约定保持一致）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g; s|https://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g; s|https://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# ---------- 2. 安装 uv（包管理器，从 PyPI 安装；GHCR 镜像国内可能不可达） ----------
# tsinghua 镜像加速；uv.lock 锁版本保证依赖可复现
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

WORKDIR /app

# ---------- 3. 依赖层（与源码分层，改代码不重装依赖） ----------
# ofdkit 是 uv workspace 成员，必须先于依赖解析拷贝（否则 uv sync 找不到成员）
COPY pyproject.toml uv.lock ./
COPY ofdkit/ ./ofdkit/
RUN uv sync --frozen --no-dev --no-install-project

# ---------- 4. 应用代码层 ----------
# alembic（启动自动迁移）/ alembic.ini / app（业务代码）/ scripts（运维脚本，可选）
# README.md 必须拷贝：pyproject 声明 readme，setuptools 构建项目本体时读取
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY README.md ./
# 安装主项目本体（workspace 根项目）
RUN uv sync --frozen --no-dev

# ---------- 5. 非 root 运行 + 数据目录 ----------
# data/ 为 local 模式原文存储根（STORAGE_ROOT），运行时以卷挂载覆盖
RUN useradd -r -u 10001 -m -d /home/appuser appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：/health 含数据库/存储连通性自检（见 app.services.startup_checks）
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 单进程多 worker：uvicorn 默认无 worker 概念，用 --workers 复用多核（SQLAlchemy
# 异步引擎按 worker 各自建连池，PG 连接数上限需相应放宽或改用 pgbouncer）
CMD ["/app/.venv/bin/uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
