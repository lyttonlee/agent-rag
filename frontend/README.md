# 企业公文知识库 · 运维台前端

PC Web 优先的运维台与配置中心（Vue3 + Vite + TS，包管理 **pnpm**）。

---

## 一、技术栈

- Vue 3 + Vite + TypeScript
- Pinia（状态）、Vue Router（路由）、Axios（请求）
- Element Plus + @element-plus/icons-vue（组件）
- ECharts（看板图表）
- 设计令牌见 `src/styles/tokens.css`（与 `docs/UI设计系统规范_V1.0.md` 对齐）

---

## 二、镜像与安装

`.npmrc` 已配置 **npmmirror**（淘宝镜像），无需手动切换。

```bash
cd frontend
pnpm install        # 安装依赖
```

---

## 三、运行

```bash
pnpm dev            # 开发服务器，默认 http://localhost:5173
pnpm build          # 类型检查(vue-tsc) + 生产构建 → dist/
pnpm preview        # 预览构建产物
pnpm typecheck      # 仅类型检查
```

### 开发代理

`vite.config.ts` 已配置：前端 `/v1` 请求自动转发到后端 `http://127.0.0.1:8000`。
启动前端前请确保后端已在 `8000` 端口运行（或相应调整代理 `target`）。

> 若对接 Mock 联调服务（`backend/mock_api/platform_mock.py`, :8800），将代理 `target` 改为 `http://127.0.0.1:8800` 即可。

---

## 四、视图（src/views）

| 视图 | 说明 |
|---|---|
| `Overview.vue` | 总览看板（KPI + 系统健康 + 文种分布），健康卡片含「对象存储 (LocalFS)」 |
| `DocLibrary.vue` | 文档库（接入/检索结果浏览） |
| `Retrieve.vue` | 检索调试（调用 `/v1/retrieve`，验证密级/权限下推） |
| `Draft.vue` | 起草辅助（调用 `/v1/generate`） |
| `Templates.vue` | 模板库（调用 `/v1/templates`） |
| `Config.vue` | 配置中心（敏感词 / 格式适配 / 模型画像，调用 `/v1/config`） |
| `Ops.vue` | 运维台（任务状态、流水线） |

---

## 五、构建产物

- 生产构建输出到 `dist/`（`pnpm build`）。
- 注意：`dist/` 为构建产物，源码修改后需重新 `pnpm build` 才会反映到 `dist/`。
- 早期静态原型位于仓库根 `web/`（仅视觉参考，非本前端运行入口）。
