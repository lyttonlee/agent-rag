# =============================================================================
# 企业公文知识库 · 项目镜像构建脚本（Windows PowerShell）
# 仅构建本项目两个镜像：backend（RAG 引擎）+ frontend（运维台）。
#
# 用法：
#   .\docker\build.ps1                 # 全部构建，版本号 1.2.0
#   $env:VERSION="1.2.1"; .\docker\build.ps1
#   .\docker\build.ps1 backend         # 只构建后端
#   .\docker\build.ps1 frontend        # 只构建前端
# =============================================================================
[CmdletBinding()]
param(
    [ValidateSet("all", "backend", "frontend")]
    [string]$Target = "all"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Version = if ($env:VERSION) { $env:VERSION } else { "1.2.0" }
$BackendImage = "gov-doc-rag-backend:$Version"
$FrontendImage = "gov-doc-rag-frontend:$Version"

function Build-Backend {
    Write-Host "==> 构建后端镜像 $BackendImage（context: backend/）"
    docker build -t $BackendImage -t "gov-doc-rag-backend:latest" -f "$PSScriptRoot\backend.Dockerfile" "$RepoRoot\backend"
    if ($LASTEXITCODE -ne 0) { throw "后端镜像构建失败" }
    Write-Host "==> 后端镜像完成：$BackendImage"
}

function Build-Frontend {
    Write-Host "==> 构建前端镜像 $FrontendImage（context: 仓库根，nginx 运行期配置在 frontend/ 之外）"
    docker build -t $FrontendImage -t "gov-doc-rag-frontend:latest" -f "$PSScriptRoot\frontend.Dockerfile" "$RepoRoot"
    if ($LASTEXITCODE -ne 0) { throw "前端镜像构建失败" }
    Write-Host "==> 前端镜像完成：$FrontendImage"
}

switch ($Target) {
    "backend"  { Build-Backend }
    "frontend" { Build-Frontend }
    "all"      { Build-Backend; Build-Frontend }
}

Write-Host @"

构建完成。启动（部署方既有 PG/模型端点）：
    cd docker; cp .env.app.example .env; notepad .env
    docker compose -f docker-compose.app.yml up -d

离线分发（内网导入）：
    docker save -o gov-doc-rag-images.tar $BackendImage $FrontendImage
    # 内网机：docker load -i gov-doc-rag-images.tar
"@
