# =============================================================================
# 企业公文知识库 · 前端镜像（运维台 + 配置中心，仅项目镜像）
#
# 构建（在仓库根目录执行，上下文为仓库根——因为运行期配置 docker/ 在 frontend/ 之外）：
#   docker build -t gov-doc-rag-frontend:1.2.0 -f docker/frontend.Dockerfile .
#
# 两阶段构建：
#   阶段一 node:22 + pnpm：安装锁版本依赖 → vue-tsc 类型检查 → vite build
#   阶段二 nginx:1.27-alpine：静态托管 dist + 反向代理 /v1 到后端服务
#
# 运行期通过环境变量指定后端地址（compose 覆盖）：
#   BACKEND_UPSTREAM=http://backend:8000   # Docker 网络内用服务名
# =============================================================================

# ---------- 阶段一：构建 ----------
FROM node:22-bookworm-slim AS builder

# pnpm 走淘宝镜像（可构建时 --build-arg PNPM_REGISTRY= 覆盖回官方源）
# 注意：registry 实际由仓库 frontend/.npmrc 指定（与本机开发一致），此处 env 仅用于
# corepack 下载 pnpm 本身；直接 `pnpm config set` 在含 pnpm-workspace.yaml 的目录会报
# "packages field missing or empty"，故删去该命令。
ARG PNPM_REGISTRY=https://registry.npmmirror.com
ENV PNPM_REGISTRY=$PNPM_REGISTRY \
    COREPACK_NPM_REGISTRY=$PNPM_REGISTRY

# pnpm 与开发机同版本（本机 11.23.0，lockfileVersion 9.0 由其维护）
RUN corepack enable && corepack prepare pnpm@11.23.0 --activate

WORKDIR /build
# 先拷依赖清单（含 .npmrc 的国内镜像配置），命中层缓存（改业务代码不重装依赖）
# pnpm-workspace.yaml 为构建脚本白名单（allowBuilds），随包一起拷贝保持一致
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/.npmrc ./
RUN pnpm install --frozen-lockfile

# 再拷源码构建（vue-tsc --noEmit 类型门禁 + vite build）
COPY frontend/tsconfig.json frontend/tsconfig.node.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src/ ./src/
RUN pnpm build

# ---------- 阶段二：运行 ----------
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="gov-doc-rag-frontend" \
      org.opencontainers.image.description="企业公文知识库 运维台与配置中心前端" \
      org.opencontainers.image.version="1.2.0"

# nginx 配置模板：启动时替换后端地址占位符（见 docker/entrypoint.sh）
COPY docker/nginx.conf /etc/nginx/templates-default.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && rm -f /etc/nginx/conf.d/default.conf

COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
