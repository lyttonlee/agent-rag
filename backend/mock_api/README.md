# Mock 接口服务（联调用）

为了让**第三方公文系统**与**部署方算力中心**在后端未完全实现时即可并行联调，
本目录提供两套独立可运行的 Mock 服务，均复用后端已有的虚拟环境（fastapi/uvicorn/python-docx）。

> 重要：两套 Mock 的字段形状以《接口文档》（`docs/企业公文知识库_接口文档.md`）v1 标准契约为准；
> 后端真实实现将在 M5/M6 与之对齐。联调请以本 Mock + 接口文档为基线。

---

## 1. 平台侧 Mock（`platform_mock.py`）

模拟我方对外暴露的 `/v1/*` 接口，供**第三方公文系统**对接测试。

### 启动
```bash
cd backend
uv run python mock_api/platform_mock.py
# 或指定端口
uv run uvicorn mock_api.platform_mock:app --port 8800
```
- 默认地址：`http://127.0.0.1:8800`
- 自动加载 3 篇种子公文（通知/报告/函，覆盖不同密级与权限），开箱即用。

### 行为要点
- 统一响应信封：`{ code, message, data, trace_id }`，`code=0` 为成功。
- `/v1/retrieve` 与 `/v1/generate` **模拟**密级 + 权限下推过滤（演示契约行为，非真实检索）：
  - 返回 `security_level` 权重 ≤ `context.max_clearance` 的切片；
  - 权限可见性（与 `app.core.security.build_visibility_filter` 语义一致）：
    - 切片**无任何权限标签**（公开/无约束）→ 对所有调用方可见（含带 `caller_scope` 者）；
    - 切片带权限标签且有 `caller_scope` → 标签交集非空才可见；
    - 切片带权限标签但无 `caller_scope` → 不可见。
  - 返回 `truncated_by_security` 标识是否有结果因密级/权限被过滤。
- `/v1/export` 返回 `{ download_url, file_id }`，`GET /v1/export/{file_id}` 返回真实 docx，便于验证下载链路。

### 快速验证
```bash
# 健康检查
curl http://127.0.0.1:8800/health

# 检索（总部公文员，可见 INTERNAL 及以下）
curl -X POST http://127.0.0.1:8800/v1/retrieve -H 'Content-Type: application/json' -d '{
  "query":"汛期值班",
  "context":{"caller_scope":["scope:dept:总部","scope:role:公文员"],"max_clearance":"INTERNAL"},
  "top_k":10
}'

# 检索（受限级，应能看到 RESTRICTED 的报告）
curl -X POST http://127.0.0.1:8800/v1/retrieve -H 'Content-Type: application/json' -d '{
  "query":"年度利润",
  "context":{"caller_scope":["scope:dept:战略发展部","scope:role:部门负责人"],"max_clearance":"RESTRICTED"},
  "top_k":10
}'
```

---

## 2. 算力中心侧参考 Mock（`compute_center_mock.py`）

实现《接口文档》第 6 章「标准模型接入接口」（OpenAI 风格），供**部署方算力中心**对齐形态，
也供我方 `ModelProvider(std)` 在真实算力中心就绪前做端到端联调。

### 启动
```bash
cd backend
uv run python mock_api/compute_center_mock.py
# 或
uv run uvicorn mock_api.compute_center_mock:app --port 8900
```
- 默认地址：`http://127.0.0.1:8900`

### 端点
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 生成（返回参考回包，非真实推理） |
| POST | `/v1/embeddings` | 向量（确定性伪向量，维度 1024） |
| POST | `/v1/rerank` | 重排（按字符重叠给分，仅联调） |

### 接入我方的方式
部署方算力中心只要暴露上述三端点（或加一层兼容网关），在我方配置中心写入一条
`provider_type=std` 的 ModelProfile（见接口文档 6.4），即可零改我方代码接入自有模型。
本地联调时可先把 ModelProfile 的 `base_url` 指向本 Mock 的 `http://127.0.0.1:8900/v1`。

---

## 3. 端点对照（与接口文档 v1）

| 接口文档端点 | 平台侧 Mock 是否实现 |
|---|---|
| `POST /v1/documents` + `GET /v1/documents/tasks/{id}` | ✅ |
| `POST /v1/retrieve` | ✅（含过滤演示） |
| `POST /v1/generate` | ✅（含引用 + 敏感词演示） |
| `GET/POST /v1/templates`、`POST /v1/templates/import` | ✅ |
| `POST /v1/export`、`GET /v1/export/{id}` | ✅ |
| `GET/PUT /v1/config/{category}` | ✅ |
| `GET /health` | ✅ |

> 说明：接口文档还定义了 `DELETE /v1/templates` 等少数端点，Mock 未逐一实现，
> 但契约形态一致，不影响联调主线。
