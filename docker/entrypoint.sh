#!/bin/sh
# =============================================================================
# 前端容器入口：渲染 nginx 配置模板 → 启动 nginx
# 环境变量：
#   BACKEND_UPSTREAM  后端 API 地址（默认 http://backend:8000）
#                     部署方若后端不在同一 compose 网络，改为宿主 IP/域名
# =============================================================================
set -e

BACKEND_UPSTREAM="${BACKEND_UPSTREAM:-http://backend:8000}"

# 仅替换占位符（envsubst 指定白名单变量，避免误伤 nginx 自身的 $uri/$host 等变量）
if command -v envsubst >/dev/null 2>&1; then
    envsubst '${BACKEND_UPSTREAM}' \
        < /etc/nginx/templates-default.conf \
        > /etc/nginx/conf.d/default.conf
else
    # 兜底：alpine 无 envsubst 时用 sed（转义 $ 防止被 shell 展开）
    sed "s|\${BACKEND_UPSTREAM}|${BACKEND_UPSTREAM}|g" \
        /etc/nginx/templates-default.conf \
        > /etc/nginx/conf.d/default.conf
fi

echo "[frontend-entrypoint] backend upstream: ${BACKEND_UPSTREAM}"

# 前台运行 nginx（容器生命周期跟随）
exec nginx -g 'daemon off;'
