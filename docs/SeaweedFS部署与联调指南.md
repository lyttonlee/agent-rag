# SeaweedFS 部署与联调指南（s3 存储模式）

> 适用：后端对象存储从默认 `local` 切换为 `s3`（SeaweedFS，Apache 2.0，S3 兼容 API）。
> 代码侧已就绪：`backend/app/services/storage.py`（S3Storage，path-style 寻址 + bucket 自动创建 + 启动自检）。
> 本文面向**部署方联调**：起服务 → 改配置 → 验证链路 → 排障。

---

## 一、部署 SeaweedFS（单机联调）

### 方式 A：docker compose（推荐，已编排）

```bash
# 在 docker/ 目录下（含 docker-compose.yml）
docker compose --profile seaweedfs up -d

# 查看状态（s3 端口 8333 就绪后继续）
docker compose ps seaweedfs
```

compose 已内置：
- 镜像 `chrislusf/seaweedfs:latest`（先按《docker_pull_list.md》在联网机器拉取并离线导入内网）；
- S3 端口 `8333`、master `9333`、filer `8888`；
- 凭证文件 `docker/seaweedfs/s3.json`（accessKey `admin` / secretKey `admin123`，**生产必改**）；
- 数据卷 `seaweed-data`。

### 方式 B：docker run（无 compose 环境）

```bash
docker run -d --name rag-seaweedfs \
  -p 8333:8333 -p 9333:9333 -p 8888:8888 \
  -v /data/seaweedfs:/data \
  -v /data/seaweedfs/s3.json:/etc/seaweedfs/s3.json:ro \
  chrislusf/seaweedfs:latest \
  server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json
```

### 凭证配置（s3.json）

```json
{
  "identities": [
    {
      "name": "rag-admin",
      "credentials": [{ "accessKey": "admin", "secretKey": "admin123" }],
      "actions": ["Admin", "Read", "Write", "List"]
    }
  ]
}
```

> 生产部署请修改 accessKey/secretKey，并与后端 `.env` 的 `STORAGE_S3_ACCESS_KEY/SECRET` 严格一致。

---

## 二、后端切换到 s3 模式

编辑 `backend/.env`（或容器环境变量）：

```ini
STORAGE_PROVIDER=s3
STORAGE_S3_ENDPOINT=127.0.0.1:8333     # SeaweedFS S3 端口；跨主机部署改为 <ip>:8333
STORAGE_S3_BUCKET=gov-doc              # 桶名；后端首次启动自动创建
STORAGE_S3_ACCESS_KEY=admin            # 与 s3.json 一致
STORAGE_S3_SECRET_KEY=admin123         # 与 s3.json 一致
STORAGE_S3_REGION=us-east-1            # SeaweedFS 不校验区域，任意值即可
STORAGE_S3_SECURE=false                # 内网 http；前置 TLS 时才设 true
STORAGE_S3_ADDRESSING_STYLE=path       # IP+端口端点必须 path；仅前置域名+泛DNS才用 virtual
```

重启后端。启动日志应出现：

```
gov_doc_rag: 对象存储就绪：s3 模式 {'endpoint': '127.0.0.1:8333', 'bucket': 'gov-doc', ...}
```

> `bucket` 不存在时后端自动 `create_bucket`，无需手工建桶（日志：`S3 bucket 已自动创建`）。

---

## 三、联调验证清单

| # | 步骤 | 命令 / 操作 | 预期 |
|---|---|---|---|
| 1 | 存储自检 | `curl http://127.0.0.1:8000/health` | `storage.provider="s3"`、`storage.ok=true` |
| 2 | 桶已建 | `docker exec rag-seaweedfs weed shell -master=localhost:9333` → `s3.bucket.list` | 列表含 `gov-doc` |
| 3 | 上传落库 | 运维台「文档库」上传一个 PDF/DOCX（或调 `POST /v1/documents/upload`） | 返回任务 ID，状态机走到 `INDEXED` |
| 4 | 对象存在 | weed shell → `s3.list` 或 AWS CLI：`aws --endpoint-url http://127.0.0.1:8333 s3 ls s3://gov-doc/raw/` | 能看到 `raw/<文档id>.<ext>` 对象 |
| 5 | 读回正常 | 前端打开该文档详情 / 下载模板原件（`GET /v1/templates/{文种}/file`） | 内容正常，无 500 |
| 6 | 切回 local（回归） | `.env` 改 `STORAGE_PROVIDER=local` 重启 | 原有 local 数据不受影响（两模式 content_ref 键格式一致：`raw/<id>.<ext>`） |

> AWS CLI 联调可选：`aws configure` 填入与 s3.json 相同的 access/secret，之后所有命令加
> `--endpoint-url http://127.0.0.1:8333`。

---

## 四、排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `对象存储不可用` | SeaweedFS 未起 / 端口不对 / 凭证不符 | 先 `curl http://127.0.0.1:8333` 探活；核对 `.env` 与 s3.json |
| `403 SignatureDoesNotMatch / InvalidAccessKeyId` | access/secret 与 s3.json 不一致，或漏配 `-s3.config` | 双方改一致；确认启动参数带 `-s3.config=/etc/seaweedfs/s3.json` |
| `BucketAlreadyExists`（跨账号） | 桶被其他身份拥有 | 换桶名 `STORAGE_S3_BUCKET`，或删除后重试 |
| 请求 502 / 连接被代理拦截 | 本机环境变量有 `http_proxy` 且未排除 127.0.0.1 | 设 `NO_PROXY=127.0.0.1,localhost`（后端进程同样需要） |
| virtual-hosted 风格 404/解析失败 | 用 IP 端点但寻址风格是 virtual | 保持 `STORAGE_S3_ADDRESSING_STYLE=path` |
| 上传 413 / 大文件慢 | SeaweedFS 默认分片 32MB 上限相关 | 单文档控制在 100MB 内；更大文件调整 volume 参数（另行评估） |

---

## 五、与 local 模式的关系

- **同一套业务代码**：`content_ref` 语义统一为"存储对象键"，ingestion / templates / export 全部经 `get_storage()` 抽象访问，切换 `STORAGE_PROVIDER` 零业务改动。
- **数据不互通**：local 的文件在 `STORAGE_ROOT` 下，s3 的对象在桶里；联调 s3 后需重新接入文档（或用 `aws s3 sync` 把 `STORAGE_ROOT/raw/` 同步进桶，键名一致即可被检索读回）。
- **选型建议**：20 万文档 / ~44GB 规模下 local 已够用且零依赖；s3 模式面向需要多实例共享存储、或部署方已有 SeaweedFS/Ceph 的场景。
