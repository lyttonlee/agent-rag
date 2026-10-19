# 项目镜像构建与部署说明（仅项目镜像）

> 本目录包含**企业公文知识库两个项目镜像**的构建文件：`backend`（RAG 引擎服务）+ `frontend`（运维台/配置中心）。
> **不含基础组件镜像**（PostgreSQL/Redis/SeaweedFS/vLLM）——那些由部署方既有环境提供，拉取清单见 `docs/docker_pull_list.md`。

## 一、文件清单

| 文件 | 说明 |
|---|---|
| `backend.Dockerfile` | 后端镜像：python:3.12-slim + uv 锁版本依赖 + app/ofdkit/alembic，启动 `uvicorn app.main:app` |
| `frontend.Dockerfile` | 前端镜像：node:22 阶段（pnpm + vue-tsc + vite build）→ nginx:1.27-alpine 阶段（静态托管 + /v1 反代）；**构建上下文为仓库根**（运行期配置 `docker/nginx.conf` 在 `frontend/` 之外，白名单式 `.dockerignore` 已防语料进上下文） |
| `nginx.conf` | nginx 配置模板（SSE 反代关缓冲、SPA 回退、64m 上传上限）；入口 `entrypoint.sh` 用 `BACKEND_UPSTREAM` 环境变量渲染 |
| `entrypoint.sh` | 前端容器入口：渲染 nginx 模板后前台运行 |
| `docker-compose.app.yml` | 项目镜像编排示例（PG/模型端点经环境变量注入，指向部署方既有实例） |
| `.env.app.example` | 编排环境变量示例（复制为 `.env` 填写） |
| `build.sh` / `build.ps1` | 一键构建脚本（Linux/macOS/Windows） |
| `Dockerfile.pg` / `docker-compose.yml` | 基础组件（PG+pgvector+zhparser 等）——**与本项目镜像互补**，开发联调用 |

## 二、构建

前置：Docker 20.10+（Linux 上 `host.docker.internal` 需 20.10+； compose v2）。

```bash
# Linux / macOS / Git Bash（在仓库根目录）
bash docker/build.sh                    # 全部，版本 1.2.0
VERSION=1.2.1 bash docker/build.sh      # 指定版本
bash docker/build.sh backend            # 只构建后端
```

```powershell
# Windows PowerShell（在仓库根目录）
.\docker\build.ps1
$env:VERSION="1.2.1"; .\docker\build.ps1 backend
```

镜像分层：后端先装依赖（uv.lock 锁死）后拷代码，改业务代码不触发依赖重装；
前端同理（先 `pnpm install --frozen-lockfile` 后 build）。

镜像 tag 约定：`gov-doc-rag-backend:<version>` / `gov-doc-rag-frontend:<version>`（同时打 `:latest`）。

## 三、运行（部署方既有 PG / 模型端点）

```bash
cd docker
cp .env.app.example .env    # 填写 PG 密码、模型端点地址、JWT_SECRET（无默认值，缺失即报错）
docker compose -f docker-compose.app.yml up -d
```

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `PG_PASSWORD` | 是 | 部署方既有 PostgreSQL 密码（实例需已装 vector/zhparser 扩展） |
| `JWT_SECRET` | 是 | JWT 签名密钥，替换默认值，部署方自行保管 |
| `PG_HOST` | 否 | 默认 `host.docker.internal`（宿主机 PG）；生产建议填内网 IP/服务名 |
| `MODEL_*_BASE_URL` | 否 | 默认指向宿主机；生产改为部署方 vLLM/算力中心内网地址 |
| `BACKEND_PORT` / `FRONTEND_PORT` | 否 | 宿主机端口，默认 8000 / 80 |

启动行为：后端 `lifespan` 自动 `alembic upgrade head`（含参考文档表等全量迁移），
无需手动执行迁移；默认管理员账号 `admin`，密码由 `ADMIN_PASSWORD` 指定（首次登录后请修改）。
健康检查：`curl http://<backend>:8000/health`（含 DB/存储连通性）。

### 3.1 存储模式

- **默认 `local`**：原文写入容器内 `/app/data`，compose 已挂 `storage-data` 卷持久化（20 万文档约 44GB，卷需落在容量充足 Disk）。
- **s3（可选）**：compose 中取消 `STORAGE_PROVIDER: s3` 段注释，指向部署方对象存储（SeaweedFS/Ceph RGW/国产对象存储，S3 兼容）。

### 3.2 SSE 流式说明

起草（ReAct）、深度推理问答、入库进度均走 SSE。nginx 反代段已关闭缓冲/缓存并放宽读超时（1h），
若部署方在前后端之间还有自建网关，需同样配置（`proxy_buffering off`）。

## 四、离线分发（纯内网）

```bash
# 联网构建机：导出
docker save -o gov-doc-rag-images.tar gov-doc-rag-backend:1.2.0 gov-doc-rag-frontend:1.2.0
# 内网机：导入后按第三节运行
docker load -i gov-doc-rag-images.tar
```

也可推送内网 Harbor 私有仓库（`docker tag` + `push`），避免逐机 load。

## 五、镜像内容边界（重要）

本项目镜像**刻意不包含**：数据库、对象存储、模型服务、训练/评测产物、
测试代码与开发依赖（pytest/ruff 等）。镜像内可执行文件仅 uvicorn + 业务代码，
数据面（原文/数据库/模型）全部外置——这与"我方是标准制定方+RAG 引擎服务，部署方适配"的
第三方接入模型一致（详见《第三方接入与使用文档》第三档）。
