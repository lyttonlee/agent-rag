# 本轮开发计划与测试方案（V1.0）

> 依据用户 2026-10-15 确认：① OFD 走「方案 B（第三方库）+ D（OCR 兜底）」；② chunk_size 走「C 改造联动」；③ 切分完整性「本次直接完成三期」。
> 本文为执行蓝图：范围、方案、测试、验收、步骤。

---

## 0. OFD 调研重大发现（需重新对齐，见 §6）

用户原选「方案 B = 第三方 OFD 库」。但调研发现 **Python 侧没有可直接安装、可用于中国 GB/T 33190 公文 OFD 的第三方库**，证据确凿：

| 候选 | 实际情况 | 结论 |
|------|----------|------|
| `ofdrw` | 是 **Java 库**（Maven `org.ofdrw`，ofdrw-core/reader/sign…），PyPI 无 Python 版；网传 `pip install ofdrw` 教程为不实内容 | ❌ 不可用（除非引入 JVM+JPype，与纯 Python 栈冲突，不推荐） |
| `ofdreader==0.1.0` | 清华镜像可装，但**只有 dist-info 元数据、无代码目录**（空包） | ❌ 空包不可用 |
| `pyofd==0.1.6` | 可装（需 setuptools+lxml 构建），但 API 是 `OFDReceipt/providers(nalog/kontur/taxcom…)`——这是**俄罗斯财政收据（фiscal data operator）**库，与 GB/T 33190 开放版式文档**同名撞车** | ❌ 场景不符 |
| `ofd` / `ofdbox` / `python-ofd` | 镜像不存在 | ❌ |

**结论**：方案 B 的前提（成熟可用 Python OFD 库）在内网镜像不成立。OFD 是**开放国标**（ZIP+XML，结构公开），文本提取用 Python 标准库 `zipfile`+`ElementTree` 即可无损完成（即原分析文档的方案 A）。自研基于开放标准、零依赖、内网最稳，且**不存在"可用轮子"故不构成重复造轮子**。

→ **需用户就 OFD 落地方式重新拍板（见 §6）**。本文其余部分（chunk_size C、切分三期）不受影响，先行开发。

---

## 1. 范围

| 项 | 内容 | 依赖 OFD 决策？ |
|----|------|----------------|
| A | 切分完整性三期（chunker + runtime_config） | 否 |
| B | chunk_size 配置联动（前端 DocLibrary + client） | 否 |
| C | OFD 解析入库（B 不可行 → 拟按 A+D） | **是（阻塞）** |
| D | 测试、报告、提交 | 否（OFD 部分随 C 后补） |

---

## 2. 切分完整性三期（一次到位）

### 一期：调配置（`runtime_config.py` 代码默认值 + 运行库 ConfigKV）

| 参数 | 现值 | 目标 | 区间 |
|------|------|------|------|
| `small_max_tokens` | 150 | **300** | 50–400 |
| `small_overlap_tokens` | 20 | **40** | 0–100 |
| `large_max_tokens` | 1200 | **2500** | 400–4000 |
| `large_overlap_tokens` | 100 | **200** | 0–400 |
| `parent_inject_max_count` | 2 | **3** | 1–8 |

> 注意：运行系统读 **DB ConfigKV**（改库即生效），改代码默认值只影响全新库。故一期需**同时**：改代码默认值 + 用脚本经配置中心 API 更新运行库值。

### 二期：单元完整性保护（条款/段落原子性，`chunker.py`）

- `_Unit` 增加 `atomic: bool`（section/paragraph 语义单元默认 `atomic=True`）。
- `_merge_budgeted` 增加 `respect_atomic: bool`：
  - 当 `respect_atomic and u.atomic and u.tokens <= max_tokens`：**先 flush 当前缓冲，再将该单元单独整块入**（不 token 窗口硬切），保护"条款/段落整体在一块里"。
  - 单元本身 > max_tokens 时仍退化硬切（无法容纳，兜底）。
- `chunk_document_dual`：small 用 `respect_atomic=False`（小块保持精召，允许超预算单元切）；large 用 `respect_atomic=True`（大块保完整条款/段落上下文）。

### 三期：section 策略进双粒度

- `chunk_document_dual` 已支持 `strategy=section`（`_units_with_offsets` 已有 `_section_units_off`）；三期为**打通 atomic + 验证**：section 单元（标题+正文）在 large 粒度整体入块。
- 文档标注：公文推荐 `section` 策略（条款级完整）；默认仍 `paragraph`（避免改动存量语义），由用户按文种选。

---

## 3. chunk_size 配置联动（方案 C，前端）

- `DocLibrary.vue` 上传表单的「切片尺寸 / 重叠字符」输入框**移除**（无效且误导）。
- 改为**只读展示当前配置中心双粒度参数**：打开上传对话框时拉 `getConfigItems('retrieval')`，展示「双粒度：小块 X token / 大块 Y token（含重叠 Y/Z）」+ 一句说明「双粒度模式下切片尺寸由配置中心统一管理」+ 「前往配置中心调整」入口。
- 提交 payload 不再传 `chunk_size`/`chunk_overlap`（后端双粒度路径本就不读）。
- `client.ts` 的 `UploadMeta.chunk_size/chunk_overlap` 标记废弃（保留字段兼容，不再由表单填充）。

---

## 4. OFD（待 §6 决策后实施；拟按 A+D）

- `parser.py`：新增 `parse_ofd_bytes`（zipfile 解包 → 定位 `OFD.xml`/`Document.xml` → 遍历页面 `Content.xml`/`Text_*.xml` 取文字 → 按页重组 → `ParsedDoc`）。
- `_sniff_format`：见 `PK` 时**解包探测根 XML 命名空间**（`ofdspec.org`）区分 OFD/DOCX（不能只看魔数）。
- `parse_bytes`：扩展名分发加 `.ofd`。
- 方案 D：无文字层 OFD 页标记 `is_scanned`/`scanned_pages`，与 PDF 扫描件策略一致，OCR 引擎复用项目二期预留（当前仅标记，不接引擎）。

---

## 5. 测试方案

| 测试 | 方法 | 通过标准 |
|------|------|----------|
| 切分-改造前后对比 | 构造含长段落/条款公文文本，跑 `chunk_document_dual`，统计段落被切比例/块数/large 是否含完整条款 | large 中"不超过预算的条款/段落"被切数 = 0；段落被切比例显著下降 |
| 切分-种子语料实测 | 对 `sichuan_gov_docs` 文本跑改造后切分，对比 small/large 块数与尺寸分布 | 块尺寸落入新预算，无异常 |
| 配置-运行库生效 | GET `/v1/config/items?category=retrieval` 核对更新后的值 | small=300/large=2500 等已生效 |
| chunk_size-前端 | `vue-tsc --noEmit` + 人工核对表单不再有切片尺寸输入 | 类型通过、表单展示配置值 |
| OFD（决策后） | 构造最小 OFD 样本（zip+XML）→ `parse_ofd_bytes` 提取文字；`.ofd`/无扩展名分发；OFD/DOCX 魔数区分 | 文字正确提取、格式正确识别、DOCX 不被误判 |
| 回归 | 现有 `verify_export/verify_steps/verify_draft_conv` 复跑 | 全绿 |

---

## 6. 需用户决策（OFD）

方案 B 不可行（§0）。请确认：

- **选项 1（推荐）**：OFD 按**方案 A（标准库自研轻量解析）+ D（OCR 兜底标记）**落地。零依赖、内网最稳、与现有公文结构识别能力对齐。
- **选项 2**：坚持引入 **Java ofdrw**（需 JVM + JPype 桥接或独立 Java 服务），接受纯 Python 栈混入 JVM 依赖的复杂度与部署成本。
- **选项 3**：暂缓 OFD，先交付其他，后续再议。

---

## 7. 实施步骤

1. 切分一期：`runtime_config.py` 默认值 + 运行库 ConfigKV 更新脚本。
2. 切分二期/三期：`chunker.py` atomic + section 打通。
3. chunk_size C：`DocLibrary.vue` + `client.ts`。
4. 测试：跑 §5 全部用例。
5. OFD：按 §6 决策实施。
6. 报告 + 提交。
