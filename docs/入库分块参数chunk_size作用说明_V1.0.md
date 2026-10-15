# 入库「分块大小（chunk_size）」参数作用说明（V1.0）

> 背景：用户在入库界面填写了「分块大小 / 重叠」，但系统已有默认的小块 150 / 大块 1200 父子分片。
> 本文解释**这个输入框在当前默认配置下到底有没有用、为什么**，以及可选的处置方案，供决策。

---

## 1. 一句话结论

**在当前默认配置下（双粒度父子分块总开关 `dual_granularity_enabled = true`），入库界面填写的「分块大小 / 重叠」是无效的——它会被系统忽略。**

系统实际用的是配置中心（`runtime_config.RetrievalConfig`）里的：

- `small_max_tokens = 150`（小块 token 上限）
- `small_overlap_tokens = 20`（小块尾重叠）
- `large_max_tokens = 1200`（大块 token 上限）
- `large_overlap_tokens = 100`（大块尾重叠）

只有当**关闭**双粒度开关（`dual_granularity_enabled = false`）时，你填的 `chunk_size` / `chunk_overlap` 才会生效（且单位是**字符**而非 token）。

---

## 2. 代码事实（为什么被忽略）

入库主链路 `services/ingestion.py` 的切片分支：

```python
# 读取配置中心
cfg = await load_retrieval_config(db)
if not cfg.dual_granularity_enabled:
    return [], [], cfg          # → 走单粒度回退
smalls, larges = chunk_document_dual(
    content, prefix, strategy,
    small_max_tokens=cfg.small_max_tokens,        # ← 用配置的 token 值
    small_overlap_tokens=cfg.small_overlap_tokens,
    large_max_tokens=cfg.large_max_tokens,
    large_overlap_tokens=cfg.large_overlap_tokens,
)
```

单粒度回退分支（仅 `dual_granularity_enabled=false` 才进）：

```python
pieces = chunk_document(
    content, prefix, strategy,
    max_chars=payload.get("chunk_size", 800),     # ← 这里才用你填的值（字符）
    overlap=payload.get("chunk_overlap", 80),
)
```

`payload["chunk_size"]` 正是前端入库表单提交的字段。它**只在上面这个 else 分支被读取**，而默认配置根本不进这个分支。

---

## 3. 为什么这样设计（不是疏漏，是路由选择）

- 双粒度（父子块 / small-to-large）是 2026-10-13 引入的**核心检索增强（P1）**：
  - 小块（150 token）入库即向量化 + 中文 BM25（`zhparser`），用于**高精度召回**；
  - 大块（1200 token）纯文本不向量化，仅在小块命中后**按需回表注入**给生成模型，用于**保上下文完整性**。
- 这两个参数**单位统一为 token**，且由配置中心管理（改库即生效、无需重启、可灰度、可 A/B），比前端表单里的字符级字段更适合做全局调优。
- 前端的 `chunk_size` 是**历史存量字段**（单粒度时代遗留），双粒度上线后语义已让位于配置中心。

> 所以：系统不是"没接收你填的值"，而是"在默认模式下这个值没有进入任何切片计算路径"。

---

## 4. 三种处置方案（请你选择）

| 方案 | 做法 | 影响 | 适用 |
|------|------|------|------|
| **A. 保持现状 + UI 提示** | 不动代码；在入库表单把「分块大小」标注为「双粒度模式下由系统配置决定，此栏不生效」或置灰 | 零风险；用户预期被纠正 | 最稳妥，推荐先落地 |
| **B. 让该栏生效（关双粒度）** | 配置中心把 `dual_granularity_enabled` 设为 `false` | 回退到单粒度检索，**丧失父子块召回增强收益**；`chunk_size`/`chunk_overlap` 以字符生效 | 若你明确不需要双粒度增强 |
| **C. 改造联动（推荐演进）** | 前端「分块大小」改为**映射/联动双粒度的 small/large token**（或改为只读展示配置中心当前值 + 跳转配置中心），由配置中心统一调参 | 一次前端小改造；参数仍走配置中心，保留双粒度收益 | 长期正确做法 |

**建议**：先上 A 消除误解；并行评估 C（把前端表单与配置中心打通，而不是引入第二套互相打架的参数）。B 仅在确认不需要双粒度召回增强时考虑。

---

## 5. 如果你选择调双粒度参数

直接改配置中心（`RetrievalConfig`）即可，无需改代码、无需重启：

- 检索想更细 → 调小 `small_max_tokens`（区间 50–400，默认 150）
- 生成上下文想更全 → 调大 `large_max_tokens`（区间 400–4000，默认 1200）
- 边界语义想更连贯 → 调大 `small_overlap_tokens` / `large_overlap_tokens`

> 注意：`small_max_tokens` 校验区间 50–400，`large_max_tokens` 验证区间 400–4000（见 `runtime_config.py` 的 `_coerce_int` 夹取），超出会被夹回边界值，请在此范围内调。
