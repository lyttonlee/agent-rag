# Docker 基础组件拉取清单与命令

> 适用：完全私有化内网部署。本文给出**联网机器上需预先拉取 / 构建的镜像清单**、**国内镜像加速配置**、**docker pull 命令**，以及**离线导出（docker save）后在纯内网导入**的做法。
> 镜像规划与版本以《详细设计文档 V1.2》为准。

---

## 一、必要基础组件清单

| 组件 | 镜像 | 用途 | 是否需构建 | 约大小 |
|---|---|---|---|---|
| PostgreSQL + pgvector + zhparser | `rag-pg:16-zh`（基于 `postgres:16-bookworm` 构建） | 元数据 + 关键词(BM25) + 向量(HNSW) 统一存储 | **是**（自定义 Dockerfile） | 基础 ~400MB + 构建层 |
| Redis | `redis:7-bookworm` | 检索缓存 / 异步任务队列（可选） | 否 | ~120MB |
| 模型服务 | `vllm/vllm-openai:latest` | Qwen3 系列（需 GPU 宿主） | 否 | ~5–8GB |
| 反向代理（可选） | `nginx:1.27-alpine` | 统一入口 / TLS | 否 | ~40MB |
| **对象存储（LocalFS，默认）** | **无需镜像** | 原文直接写后端挂载卷（`STORAGE_ROOT`），零镜像、零协议风险 | 否 | 0 |
| 对象存储（SeaweedFS，可选 s3 模式） | `chrislusf/seaweedfs:latest` | S3 兼容对象存储（Apache 2.0，替代 MinIO）；仅当采用 s3 模式时拉取 | 否 | ~80MB |

> **MinIO 已被替换**：原方案使用 MinIO 作为对象存储，因其开源协议（AGPLv3 且社区版功能裁剪趋势）及镜像获取受限，改为**存储抽象层**：
> - **默认 `local` 模式**——本地文件系统，零镜像、零协议风险，适合本项目 20 万文档 / ~44GB 规模；
> - **可选 `s3` 模式**——指向 **SeaweedFS**（Apache 2.0，镜像可得）或 Ceph RGW / 国产私有化对象存储（均兼容 S3 API）。
> 业务代码通过 `STORAGE_PROVIDER=local|s3` 切换，**零改动**。详见《详细设计文档 V1.2》与 `backend/app/services/storage.py`。

> 说明：zhparser 需编译，故 PostgreSQL 采用**自定义构建镜像**而非直接拉官方镜像。
> 若 zhparser 构建受阻，可降级为「只向量 + 简单中文分词（jieba 客户端侧）」，检索质量略降但不阻塞 MVP。

---

## 二、国内镜像加速（在联网机器上配置）

编辑 `/etc/docker/daemon.json`（Windows Docker Desktop：Settings → Docker Engine），加入 `registry-mirrors`：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://hub-mirror.c.163.com",
    "https://mirror.baidubce.com"
  ],
  "insecure-registries": []
}
```

重启 Docker 后生效：`systemctl restart docker`（Linux）或在 Desktop 点击 Apply。

> 校验：`docker info | grep -A3 Registry Mirrors` 能看到上述地址即生效。

---

## 三、拉取命令清单（在联网机器执行）

```bash
# 1) PostgreSQL 基础镜像（用于构建自定义 rag-pg:16-zh）
docker pull postgres:16-bookworm

# 2) 构建自定义 PG 镜像（含 pgvector + zhparser + 中文分词配置）
#    构建机需能访问 github（拉取 pgvector / scws / zhparser 源码）
docker build -t rag-pg:16-zh -f docker/Dockerfile.pg docker/

# 3) Redis（可选）
docker pull redis:7-bookworm

# 4) 模型服务（需 GPU 宿主，体积大，建议按需拉取）
docker pull vllm/vllm-openai:latest

# 5) 反向代理（可选）
docker pull nginx:1.27-alpine

# 6) 【可选】若采用 s3 对象存储模式，拉取 SeaweedFS（替代 MinIO，Apache 2.0）
docker pull chrislusf/seaweedfs:latest
```

> 默认 local 模式**无需拉取任何对象存储镜像**。

---

## 四、离线分发（纯内网）

在联网机器把镜像导出为 tar，拷贝到内网后导入：

```bash
# 联网机：导出（一次导出多个）
docker save -o rag-images.tar \
  rag-pg:16-zh \
  redis:7-bookworm \
  vllm/vllm-openai:latest \
  nginx:1.27-alpine
  # chrislusf/seaweedfs:latest   # 仅 s3 模式追加

# 内网机：导入
docker load -i rag-images.tar
```

> 也可在内网搭建私有镜像仓库（Harbor / 阿里云企业版），`docker tag` 后 `push` 到内网 registry，避免逐机 load。

---

## 五、启动顺序与验证

```bash
cd docker
cp .env.example .env          # 按需修改账号密码

# 默认 local 模式：只需 PG + Redis（可选）+ 模型服务
docker compose up -d pg redis

# 若采用 s3 模式，改用 SeaweedFS（取消 compose 中 seaweedfs 注释）：
# docker compose up -d pg redis seaweedfs

# 验证 PG 扩展已创建
docker exec -i rag-pg psql -U rag -d gov_doc_rag -c "\dx"   # 应见 vector / zhparser
# 验证 Redis 可用
docker exec -i rag-redis redis-cli ping                      # 应返回 PONG
```

模型服务（vLLM）在具备 GPU 的宿主单独启动，详见 compose 中注释段落。

---

## 六、与后端配置对应关系

| 后端配置项 | 默认值 | 对应组件 |
|---|---|---|
| `PG_HOST/PORT/USER/PASSWORD/DATABASE` | 127.0.0.1:5432 / rag / gov_doc_rag | rag-pg:16-zh |
| `STORAGE_PROVIDER` | `local` | 本地文件系统（无需镜像）；`s3` 时指向 SeaweedFS 等 |
| `STORAGE_ROOT` | `./data/gov-doc` | local 模式根目录（后端容器挂载 `storage-data` 卷） |
| `STORAGE_S3_*` | 127.0.0.1:8333 / gov-doc / admin | s3 模式：SeaweedFS / Ceph / 国产对象存储 |
| `REDIS_URL` | redis://127.0.0.1:6379/0 | redis |
| `MODEL_*_BASE_URL` | http://127.0.0.1:8000/v1 | vllm（local/std） |
