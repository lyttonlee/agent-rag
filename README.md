# 企业公文知识库 · RAG 引擎平台

> 我方定位：**标准制定方 + RAG 引擎服务**。接口规范、字段标准、密级/权限枚举、模型接入标准由我方制定，第三方公文系统（部署方）按标准适配接入。

企业级公文知识库平台：为第三方公文系统提供**文档解析 + 切片入库 + 检索推理**的标准化 RAG 引擎，并配套模板库、导出下载接口、配置中心与运维台。检索/推理在**服务端强制执行密级 + 权限下推过滤**，涉密内容在数据库层即被排除，不进入模型上下文。

---

## 一、项目状态（2026-10-10）

| 项 | 状态 |
|---|---|
| 已部署基础组件 | ✅ `192.168.6.49` 上 **PostgreSQL(pgvector+zhparser)** 与 **Redis** 已就绪 |
| 目标库 | `gov_doc_rag`（初始为空，可经 Alembic 自动建表 / 触发器） |
| 对象存储 | ✅ 已**去 MinIO 化**，改为「存储抽象层」（默认 LocalFS，可选 S3 兼容） |
| 后端骨架 | ✅ FastAPI 模块化单体 + Alembic 迁移 + Mock 联调服务 |
| 真实库冒烟测试 | ✅ 针对 `192.168.6.49` 跑通，`scripts/smoke_remote.py` **12/12 通过** |
| 前端运维台 | ✅ Vue3 + Vite + TS 骨架完成，可独立运行 |
| 设计 / 接口文档 | ✅ `docs/` 下 4 份设计文档 + 接口文档 + 对接指南 + UI 规范 |

> 说明：M5/M6（真实检索/生成实现）尚未开始，当前后端的检索/生成以 Mock + 真实库字段验证为主。

---

## 二、架构总览

```
第三方公文系统 ──(网关注入 caller_scope/max_clearance)──▶ 我方 RAG 引擎(API)
                                                          │
        ┌─────────────────────────────────────────────────┤
        ▼                ▼                ▼               ▼
   PostgreSQL+pgvector  对象存储(抽象层)   模型 Provider     配置中心
   (BM25+向量+RRF+rerank) (LocalFS/S3)   (local/cloud/std)  (敏感词/模板/模型画像)
        ▲
   密级/权限 WHERE 下推（服务端强制执行，不依赖第三方"不问就不给"）
```

- **后端**：`backend/`（FastAPI，模块化单体，Docker Compose 部署，不用 K8s）
- **前端运维台**：`frontend/`（Vue3 + Vite + TS，PC Web 优先）
- **Mock 联调**：`backend/mock_api/`（平台侧 + 算力中心侧两套，供第三方并行联调）
- **容器**：`docker/`（自定义 PG 镜像 pgvector+zhparser、compose）
- **文档**：`docs/`（需求 / 设计 / 接口 / 对接 / UI 规范）
- **种子语料**：`sichuan_gov_docs/`（30 篇四川省政府公开版/删减版 PDF）
- **工具**：`tools/`（样本体检脚本）

---

## 三、仓库目录

```
agent-rag/
├── README.md                 # 本文件（项目总览）
├── backend/                  # 后端 FastAPI 服务
│   ├── README.md             # 后端详细文档（配置/运行/迁移/测试）
│   ├── pyproject.toml        # 依赖（uv 管理，已配清华镜像）
│   ├── .env.example          # 环境变量样例
│   ├── app/                  # 源码：core/db/schemas/services/api
│   ├── alembic/              # 数据库迁移
│   ├── mock_api/             # 联调 Mock 服务
│   └── scripts/              # smoke_remote.py 等脚本
├── frontend/                 # 运维台前端（Vue3 + Vite + TS，pnpm 管理）
├── web/                      # 早期静态原型（仅视觉参考，非运行入口）
├── docker/                   # PG 自定义镜像 + docker-compose
├── docs/                     # 需求/设计/接口/对接/UI 规范
├── sichuan_gov_docs/         # 种子语料
└── tools/                    # 样本体检脚本
```

---

## 四、快速开始

### 4.1 后端

```bash
cd backend
uv venv --python "C:/path/to/python3.13.12/python.exe"   # 首次：创建隔离环境
uv sync --group dev                                       # 安装依赖（含开发组）
cp .env.example .env                                      # 按需修改

# 开发模式（自动建表 + 触发器）
uv run uvicorn app.main:app --reload --port 8000
# 接口文档：http://127.0.0.1:8000/docs
```

> 更完整的配置项、存储抽象层、Alembic 迁移、冒烟测试见 **`backend/README.md`**。

### 4.2 前端运维台

```bash
cd frontend
pnpm install          # 已配 npmmirror；见 frontend/README.md
pnpm dev              # 开发服务器（默认代理 /v1 → 后端 8000）
pnpm build            # 类型检查 + 生产构建
```

### 4.3 Mock 联调（无需后端真实实现即可并行联调）

```bash
cd backend
uv run python mock_api/platform_mock.py        # 平台侧，:8800（第三方对接）
uv run python mock_api/compute_center_mock.py  # 算力中心侧，:8900（模型接入对齐）
```

---

## 五、对象存储说明（去 MinIO 化）

**背景**：原方案使用 MinIO，因开源协议（AGPLv3 及社区版裁剪趋势）与镜像获取受限，改为**存储抽象层**。

| 模式 | 说明 | 适用 |
|---|---|---|
| `local`（默认） | 直接读写本地文件系统，**零镜像、零协议风险**；适配 20 万文档 / ~44GB 单节点内网 | ✅ 默认启用 |
| `s3` | 经 `boto3` 对接 S3 兼容存储：SeaweedFS（Apache-2.0，~80MB）/ Ceph RGW / 国产对象存储 | 需 S3 语义或容量扩展时 |

切换只需改 `.env` 的 `STORAGE_PROVIDER` 及 `STORAGE_S3_*`，`content_ref` 字段语义统一为「存储对象键」，业务代码零改动。详见 `backend/README.md` 与 `docs/docker_pull_list.md`。

---

## 六、测试

### 6.1 真实环境冒烟（已部署 `192.168.6.49`）

`backend/scripts/smoke_remote.py` 直接连真实 PG/Redis，覆盖：本地存储读写 + 路径穿越防护、3 篇不同密级/权限文档入库 + 原文持久化、zhparser 中文分词召回、3 种密级/权限下推上下文、敏感词掩码端到端。

```bash
cd backend
PG_HOST=192.168.6.49 PG_PORT=5432 PG_USER=rag PG_PASSWORD=rag123456 PG_DATABASE=gov_doc_rag \
REDIS_HOST=192.168.6.49 REDIS_PORT=6379 \
STORAGE_PROVIDER=local STORAGE_ROOT=/tmp/gov-doc-rag-smoke \
uv run python -m scripts.smoke_remote
```

> ⚠️ 上述为测试环境凭据，**生产环境务必更换**，并配合网络封死出口（涉密不出网）。

### 6.2 其它

- 单元测试：待补充（M5/M6 阶段）。
- 样本体检：`tools/sample_audit.py`（验证种子语料文字层覆盖率等）。

---

## 七、文档索引（`docs/`）

| 文档 | 说明 |
|---|---|
| `企业公文知识库_详细设计文档_V1.2.md` | 现行总设计（取代 V1.0/V1.1） |
| `企业公文知识库_项目架构文档.md` | 系统架构与部署 |
| `企业公文知识库_详细功能设计文档.md` | 功能模块设计 |
| `企业公文知识库_接口文档.md` | 对外 / 模型接入标准契约（含字段/密级/权限枚举） |
| `第三方系统对接指南.md` | 第三方公文系统接入指引 |
| `企业公文智能写作知识库平台_需求规格说明书.md` | 需求基线 |
| `企业公文知识库_MVP详细设计方案_V1.0.md` | MVP 范围 |
| `企业公文知识库_技术方案V1.0_选型确认与信创适配.md` | 选型与信创适配 |
| `UI设计系统规范_V1.0.md` | 前端设计令牌与规范 |
| `docker_pull_list.md` | 基础组件镜像拉取清单与命令 |

---

## 八、关键约束

- **密级 / 权限过滤必须在检索阶段下推到数据库 WHERE**，涉密 / 越权内容不进入模型上下文。
- **涉密不出网三层防护**：Provider 隔离 + 启动自检（生产拒绝云端）+ 网络封死出口。
- 仅内网企业秘密（非国家秘密），轻量密级控制，非分保级。
- 我方不重认证终端用户，信任上游网关注入的 `caller_scope` + `max_clearance`，但过滤**服务端强制执行**。

---

## 九、常见问题

**Q：为什么不用 MinIO？**
A：开源协议与镜像获取受限，且对象存储在本项目仅为「原文二进制落盘」，无强一致/多节点刚需，故用存储抽象层（默认 LocalFS，零风险）。需要 S3 时切 `s3` 模式指向 SeaweedFS / 国产 OSS 即可。

**Q：前端 `dist/` 仍显示 MinIO？**
A：`dist/` 是旧构建产物，源码 `frontend/src/views/Overview.vue` 已改为「对象存储 (LocalFS)」，执行 `pnpm build` 重新构建即可。

**Q：如何验证权限下推？**
A：跑 `smoke_remote.py`（真实库）或 `mock_api/platform_mock.py` 的 `/v1/retrieve`（公开/空标签切片对带 scope 调用方也可见）。
