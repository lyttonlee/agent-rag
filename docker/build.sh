#!/usr/bin/env bash
# =============================================================================
# 企业公文知识库 · 项目镜像构建脚本（Linux/macOS，Git Bash 亦可）
# 仅构建本项目两个镜像：backend（RAG 引擎）+ frontend（运维台）。
# 基础组件（PG/Redis/SeaweedFS/vLLM）不在本脚本范围，由部署方既有环境提供。
#
# 用法：
#   bash docker/build.sh                 # 全部构建，版本号 1.2.0
#   VERSION=1.2.1 bash docker/build.sh   # 指定版本号
#   bash docker/build.sh backend         # 只构建后端
#   bash docker/build.sh frontend        # 只构建前端
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."          # 仓库根目录

VERSION="${VERSION:-1.2.0}"
TARGET="${1:-all}"

BACKEND_IMAGE="gov-doc-rag-backend:${VERSION}"
FRONTEND_IMAGE="gov-doc-rag-frontend:${VERSION}"

build_backend() {
    echo "==> 构建后端镜像 ${BACKEND_IMAGE}（context: backend/）"
    docker build \
        -t "${BACKEND_IMAGE}" \
        -t "gov-doc-rag-backend:latest" \
        -f docker/backend.Dockerfile \
        backend/
    echo "==> 后端镜像完成：${BACKEND_IMAGE}"
}

build_frontend() {
    echo "==> 构建前端镜像 ${FRONTEND_IMAGE}（context: 仓库根，nginx 运行期配置在 frontend/ 之外）"
    docker build \
        -t "${FRONTEND_IMAGE}" \
        -t "gov-doc-rag-frontend:latest" \
        -f docker/frontend.Dockerfile \
        .
    echo "==> 前端镜像完成：${FRONTEND_IMAGE}"
}

case "${TARGET}" in
    backend)  build_backend ;;
    frontend) build_frontend ;;
    all)      build_backend && build_frontend ;;
    *) echo "用法: $0 [all|backend|frontend]" >&2; exit 1 ;;
esac

cat <<EOF

构建完成。启动（部署方既有 PG/模型端点）：
    cd docker && cp .env.app.example .env && vi .env
    docker compose -f docker/docker-compose.app.yml up -d

离线分发（内网导入）：
    docker save -o gov-doc-rag-images.tar ${BACKEND_IMAGE} ${FRONTEND_IMAGE}
    # 内网机：docker load -i gov-doc-rag-images.tar
EOF
