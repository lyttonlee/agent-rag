# 本轮成果报告：切分三期 + chunk_size 联动 + OFD 调研（V1.0）

> 日期：2026-10-15 ｜ 依据用户确认：OFD 走「B+D」、chunk_size 走「C」、切分「本次完成三期」。
> 本报告含：执行计划回顾、开发过程记录、测试结果、OFD 重大发现与待决策项。

---

## 1. 执行计划回顾

计划见 `docs/本轮开发计划与测试方案_OFD_切分_chunksize_V1.0.md`。分四项：
- A. 切分完整性三期（chunker + runtime_config）—— ✅ 已完成
- B. chunk_size 配置联动 C（前端）—— ✅ 已完成
- C. OFD 解析入库（B+D）—— ⚠️ 方案 B 前提不成立，**待重新决策**（见 §4）
- D. 测试、报告、提交 —— ✅ 进行中

---

## 2. 开发过程记录

### 2.1 切分一期：调配置

- `backend/app/services/runtime_config.py` 代码默认值上调（附详细注释）：
  - `small_max_tokens` 150 → **300**；`small_overlap_tokens` 20 → **40**
  - `large_max_tokens` 1200 → **2500**；`large_overlap_tokens` 100 → **200**
  - `parent_inject_max_count` 2 → **3**；`parent_inject_max_tokens` 3000 → **6000**（大块调大后需容纳约 2 个大块）
- **运行库同步更新**：`tools/update_retrieval_config.py` 经配置中心 API 写入当前库 ConfigKV（运行系统读 DB，仅改代码默认值不影响已建库）。回读校验 6/6 一致。

### 2.2 切分二期：句边界软切分（`chunker.py`）

- 根因：真正切碎语义的是「超预算单元走 `_token_windows` 在任意 token 处硬切」；未超预算单元本就整块入。
- 新增 `_sentence_spans`（按 。！？；\n 切句并定位）与 `_soft_windows`（句边界打包成块，单句仍超预算才对该句 token 硬切兜底）。
- `_merge_budgeted` 中超预算分支由 `_token_windows` 改为 `_soft_windows`。块间不重复（无滑动重叠，避免同句跨块冗余），偏移取句子精确 span。

### 2.3 切分三期：section 策略进双粒度

- `chunk_document_dual` 早已支持 `strategy=section`（`_section_units_off` 产出「标题+正文」单元），且与 paragraph 走同一 `_merge_budgeted`；本期通过软切分打通其超预算场景，并实测两种策略效果（见 §3）。
- 语义说明：公文推荐 `section`（条款级完整）；入库默认仍 `paragraph`（不改存量语义），由用户按文种在下拉选择。

### 2.4 chunk_size 配置联动 C（前端）

- `frontend/src/views/DocLibrary.vue`：
  - 移除上传表单「切片尺寸 / 重叠字符」两个输入框（双粒度下无效且误导）。
  - 改为打开上传框时 `getConfigItems('retrieval')` 拉取并**只读展示**当前双粒度参数（小块/大块 token 及重叠）+ 引导「配置中心 · 检索配置」。
  - 提交 payload 不再传 `chunk_size`/`chunk_overlap`。
- `frontend/src/api/client.ts`：`UploadMeta.chunk_size/chunk_overlap` 标注 `@deprecated`（保留兼容单粒度回退路径）。

---

## 3. 测试结果

| 测试 | 工具 | 结果 |
|------|------|------|
| 软切 vs 硬切（单个 157-token 超预算单元，预算 60） | `tools/verify_chunk_completeness.py` | 硬切 3 片**腰斩 2 处**；软切 4 片**腰斩 0 处** ✅ |
| 新参数双粒度（small=300/large=2500） | 同上 | 段落/章节两种策略：大块均 **0 腰斩**，整篇（539/535 token）**1 片容纳** ✅ |
| 配置写入运行库 | `tools/update_retrieval_config.py` | PUT 6/6 成功，回读全部一致 ✅ |
| 回归：检索+生成步骤事件 | `tools/verify_steps.py` | plan/step_start/step_end/meta 正常流出 ✅ |
| 回归：导出（上轮） | `tools/verify_export.py` | 8/8 PASS（本轮未改动导出，仅防回归）✅ |
| 前端类型检查 | `vue-tsc --noEmit` | EXIT=0 ✅ |
| 后端模块导入 | `python -c import` | chunker/ingestion/runtime_config 导入 OK ✅ |

> 说明：切分改动只影响**新入库**文档的切片；存量已索引文档不受影响（与配置中心语义一致）。

---

## 4. OFD 重大发现与待决策（阻塞项）

用户原选「方案 B = 第三方 OFD 库」。调研发现 **Python 侧没有可用于中国 GB/T 33190 公文 OFD 的第三方库**，证据确凿：

| 候选 | 实际情况 |
|------|----------|
| `ofdrw` | 是 **Java 库**（Maven `org.ofdrw`），PyPI 无 Python 版；网传 `pip install ofdrw` 为不实内容 |
| `ofdreader==0.1.0` | 清华镜像可装，但**只有 dist-info 无代码**（空包） |
| `pyofd==0.1.6` | 可装，但 API 是 `OFDReceipt/providers(nalog/kontur/taxcom…)`——**俄罗斯财政收据库**，与 GB/T 33190 同名撞车 |
| `ofd/ofdbox/python-ofd` | 镜像不存在 |

**结论**：方案 B 前提不成立。OFD 是开放国标（ZIP+XML，结构公开），文本提取用 Python 标准库 `zipfile`+`ElementTree` 即可无损完成（即分析文档的方案 A）。自研零依赖、内网最稳、与现有公文结构识别能力对齐，且不存在"可用轮子"故不构成重复造轮子。

→ **需你就 OFD 落地方式拍板**（选项见交互提问）：A+D（推荐）/ 坚持 Java ofdrw / 暂缓。

---

## 5. 遗留与后续

1. **OFD（#108）**：待 §4 决策后实施（拟 A+D：`parse_ofd_bytes` + `_sniff_format` 解包命名空间区分 OFD/DOCX + 无文字层标记 scanned）。
2. **存量文档**：若希望已入库文档也按新参数重切，需重跑入库（可选，非必须）。
3. **检索准确率观测**：小块由 150→300，建议在评测台观察精召变化（small-to-large 固有权衡）。

---

## 6. 变更文件清单（本次提交）

**后端**
- `app/services/runtime_config.py`（切分默认值 + parent_inject 预算）
- `app/services/chunker.py`（句边界软切分 `_soft_windows`/`_sentence_spans`）
- `app/api/routes/export.py`、`app/schemas/contracts.py`、`app/services/generation.py`（上轮导出/步骤/会话修复）

**前端**
- `views/DocLibrary.vue`（chunk_size 联动 C；另含上轮新建空间修复）
- `views/Draft.vue`、`components/AgentStepPanel.vue`、`api/client.ts`（上轮步骤/导出；本轮 deprecate 标注）

**文档**
- `docs/本轮开发计划与测试方案_OFD_切分_chunksize_V1.0.md`
- `docs/入库分块参数chunk_size作用说明_V1.0.md`
- `docs/OFD文件解析入库方案对比_V1.0.md`
- `docs/文档切分完整性问题分析与解决方案_V1.0.md`
- `docs/企业公文知识库_导出功能重设计方案_V1.0.md`（上轮）

**工具**
- `tools/update_retrieval_config.py`、`tools/verify_chunk_completeness.py`（本轮）
- `tools/verify_export.py`、`tools/verify_steps.py`、`tools/verify_draft_conv.py`（上轮）
