# 企业公文知识库 · 后端服务（RAG 引擎）

模块化单体（FastAPI）。被第三方公文系统集成的标准化 RAG 引擎：**文档解析 + 切片入库 + 检索推理**，配套模板库、导出下载接口、配置中心。

---

## 一、技术栈

- Python 3.11+（开发使用隔离解释器 3.13.12），包管理 **uv**
- FastAPI / SQLAlchemy 2.x(async) / asyncpg / pgvector
- PostgreSQL + pgvector + zhparser（混合检索：BM25 + 向量 + RRF + rerank）
- 对象存储**抽象层**（默认本地文件系统；可选 S3 兼容 SeaweedFS / Ceph RGW / 国产对象存储）
- 模型：`ModelProvider` 抽象（local = 我方 Qwen3 栈 vLLM / cloud = 开发云端 / std = 部署方算力中心，均 OpenAI 兼容）
- 国内镜像：`pyproject.toml` 已配置清华 PyPI（`[tool.uv] index-url`）

---

## 二、配置参考（环境变量）

复制 `.env.example` 为 `.env` 后按需修改。下表为全部字段：

> **`.env` 定位**：固定读取 `backend/.env`（按代码路径解析，与进程 CWD 无关）。
> 从任意目录启动 `uvicorn app.main:app` 都能正确加载；环境变量优先级仍高于 `.env`。

| 分组 | 变量 | 默认 | 说明 |
|---|---|---|---|
| 基础 | `APP_NAME` / `APP_VERSION` / `ENVIRONMENT` / `LOG_LEVEL` | `GovDoc-RAG` / `1.2.0` / `dev` / `INFO` | 应用元信息；`ENVIRONMENT=production` 触发云端 Provider 启动自检拒绝 |
| PostgreSQL | `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DATABASE` | `127.0.0.1` / `5432` / `rag` / `rag123456` / `gov_doc_rag` | 数据库连接；含 pgvector + zhparser |
| 存储（抽象层） | `STORAGE_PROVIDER` | `local` | `local` 或 `s3`（二者等价，均走 S3Storage 后端，可指向 SeaweedFS/Ceph RGW/国产对象存储） |
| | `STORAGE_ROOT` | `./data/gov-doc` | `local` 模式根目录 |
| | `STORAGE_S3_ENDPOINT` / `STORAGE_S3_BUCKET` / `STORAGE_S3_ACCESS_KEY` / `STORAGE_S3_SECRET_KEY` / `STORAGE_S3_REGION` / `STORAGE_S3_SECURE` | `127.0.0.1:8333` / `gov-doc` / `admin` / `admin123` / `us-east-1` / `false` | 仅 `s3` / `SeaweedFS` 模式使用 |
| 模型·生成 | `MODEL_GEN_BASE_URL` / `MODEL_GEN_API_KEY` / `MODEL_GEN_NAME` | `http://127.0.0.1:8000/v1` / `EMPTY` / `qwen3-32b-instruct` | OpenAI 兼容 chat 端点 |
| 模型·向量 | `MODEL_EMB_BASE_URL` / `MODEL_EMB_API_KEY` / `MODEL_EMB_NAME` / `EMBEDDING_DIM` | `http://127.0.0.1:8001/v1` / `EMPTY` / `qwen3-embedding-4b` / `1024` | 离线 Embedding |
| 模型·重排 | `MODEL_RERANK_BASE_URL` / `MODEL_RERANK_API_KEY` / `MODEL_RERANK_NAME` | `http://127.0.0.1:8002/v1` / `EMPTY` / `qwen3-reranker-0.6b` | 在线 Rerank |
| 安全 | `FORBID_CLOUD_IN_PRODUCTION` | `true` | 生产环境拒绝云端 Provider（涉密不出网） |

> Redis 用于异步任务 / 缓存，连接参数由代码常量或后续扩展的环境变量提供（当前冒烟测试通过 `REDIS_HOST`/`REDIS_PORT` 注入）。

---

## 三、对象存储抽象层（去 MinIO 化）

实现于 `app/services/storage.py`：

- `StorageProvider`（ABC）：`save / get / delete / exists / size`。
- `LocalFSStorage`（默认）：原子写（临时文件 + rename）、**路径穿越防护** `_safe_path`（拒绝 `..`、绝对路径、根逃逸键）。
- `S3Storage`：`boto3` 惰性导入，对接任意 S3 兼容端点。
- `get_storage()`：按 `settings.storage_provider` 返回单例；`reset_storage()`：测试用重置。
- 语义统一：`content_ref` = 存储对象键（`local` 相对路径 / `s3` 对象键）。

切换示例（改 `.env`）：

```ini
STORAGE_PROVIDER=s3
STORAGE_S3_ENDPOINT=192.168.6.49:8333
STORAGE_S3_BUCKET=gov-doc
STORAGE_S3_ACCESS_KEY=admin
STORAGE_S3_SECRET_KEY=admin123
STORAGE_S3_REGION=us-east-1
STORAGE_S3_SECURE=false
```

---

## 四、数据库迁移（Alembic）

```bash
cd backend
uv run alembic upgrade head     # 应用全部迁移（建表 + zhparser 配置 + search_vec 触发器）
```

- `alembic/versions/0001_initial.py`：基础表 + `chinese` 全文检索配置（zhparser 解析器）。
  - ⚠️ PostgreSQL 不支持 `CREATE TEXT SEARCH CONFIGURATION IF NOT EXISTS`，迁移采用 `DROP ... IF EXISTS` 后 `CREATE`。
- `alembic/versions/0002_search_vec_trigger.py`：`chunks.search_vec` 的 BEFORE INSERT/UPDATE 触发器（自动维护 tsvector）+ 一次性回填。

**启动自动建表/更新**：应用启动时自动执行 `alembic upgrade head`（`app/db/migrate.py`），
覆盖"创建或更新"两种语义——`create_all` 只能建缺失表、无法加列/改索引。
多进程场景用 PG 咨询锁（`pg_advisory_lock` + `lock_timeout`）保证只有一个进程执行迁移，
抢不到锁的进程跳过（对方在迁，结果一致）。dev/test 环境迁移异常时兜底 `create_all`
（保证起得来，但会打 WARNING）；**生产不兜底**，schema 漂移比启动失败更危险。

---

## 五、启动自检（数据库 / 存储 / Embedding / Rerank）

启动时对四项外部依赖做**真实探测**（连通 + 认证 + 最小请求，与业务调用同源）：

| 依赖 | 探测方式 | 失败影响 |
|---|---|---|
| 数据库 | `SELECT 1` + 版本/表数/alembic 版本 | **致命**：启动中止并给出排障提示 |
| 对象存储 | `storage_info()`（local 探根目录可写；s3 探 head_bucket） | 降级：WARNING，服务仍起 |
| Embedding | 真实 `embed()` 一次，校验维度 = `EMBEDDING_DIM` | 降级：WARNING |
| Rerank | 真实 `rerank()` 一次，验证端点 + 模型名 | 降级：WARNING（检索层回退 RRF） |

排障入口：

- `GET /health`：进程状态 + 数据库/存储实时探测 + **上次启动自检结果缓存**；
- `GET /health/check`：**实时复检**四项（改完 `.env` 或重启依赖服务后无需重启后端即可核验）。

常见失败：rerank/embedding 报 404 多为**模型名与服务端 `/v1/models` 列表不一致**
（用 `curl $BASE_URL/v1/models` 核对）；数据库连接被拒查 PG 状态、端口放通、`pg_hba.conf`。

---

## 六、运行

```bash
cd backend
uv venv --python "C:/path/to/python3.13.12/python.exe"
uv sync --group dev
cp .env.example .env
uv run uvicorn app.main:app --reload --port 8000
# OpenAPI 文档：http://127.0.0.1:8000/docs
```

> 应用当前暴露 11 条路由（健康检查 + 文档接入/检索/生成/模板/导出/配置等占位/骨架）。真实检索/生成实现见 M5/M6。

---

## 七、测试

### 6.1 真实环境冒烟（推荐，已部署 `192.168.6.49`）

`scripts/smoke_remote.py` 连接真实 PG/Redis，并对 `ModelProvider.embed/rerank/generate` 打桩（不依赖真实模型），覆盖端到端字段与过滤行为：

```bash
cd backend
PG_HOST=192.168.6.49 PG_PORT=5432 PG_USER=rag PG_PASSWORD=rag123456 PG_DATABASE=gov_doc_rag \
REDIS_HOST=192.168.6.49 REDIS_PORT=6379 \
STORAGE_PROVIDER=local STORAGE_ROOT=/tmp/gov-doc-rag-smoke \
uv run python -m scripts.smoke_remote
```

用例（当前 **12/12 通过**）：本地存储读写 + 穿越防护、3 篇不同密级/权限文档入库 + 原文持久化、zhparser 中文分词 `search_vec` 召回、3 种密级/权限下推上下文、敏感词单元 + 端到端掩码。

### 6.2 单元测试 / 其它

- 单元与集成测试：M5/M6 阶段补充。
- 样本体检：`tools/sample_audit.py`（种子语料文字层覆盖率等）。

---

## 八、Mock 联调服务

见 `mock_api/README.md`：

- `platform_mock.py`（:8800）：模拟我方 `/v1/*`，供**第三方公文系统**对接。
- `compute_center_mock.py`（:8900）：实现《接口文档》第 6 章标准模型接入接口（OpenAI 风格），供**部署方算力中心**对齐。

---

## 九、目录结构

```
backend/
├── app/
│   ├── core/        配置(config)、安全(security：密级/权限下推)
│   ├── db/          模型(models)、会话(session)
│   ├── schemas/     请求/响应契约（对齐《接口文档》）
│   ├── services/    解析、切片、入库(ingestion)、检索(retrieval)、
│   │                生成、模型 Provider、存储抽象(storage)
│   └── api/         REST 路由（/v1/*）
├── alembic/         数据库迁移
├── mock_api/        联调 Mock 服务 + README
├── scripts/         smoke_remote.py 等
├── pyproject.toml   依赖（uv，清华镜像）
└── .env.example     环境变量样例
```

---

## 十、关键约束

- 密级 / 权限过滤**必须在检索阶段下推到数据库 WHERE**（见 `app/core/security.py` 的 `build_visibility_filter`），涉密 / 越权内容不进入模型上下文。
- 公开 / 无权限约束（空 `permission_tags`）的切片对**所有调用方可见**（含带 `caller_scope` 者）。
- 涉密不出网三层防护：Provider 隔离 + 启动自检（生产拒绝云端）+ 网络封死出口。
