# ofdkit — OFD（GB/T 33190）文本内容提取库

> 企业公文知识库的 OFD 解析独立库。纯标准库实现（zipfile + ElementTree），
> **零三方依赖**，可整体拷贝到任意 Python 3.11+ 环境独立使用。

## 能力

| 能力 | 说明 |
|---|---|
| ZIP 包导航 | `OFD.xml → DocRoot → Document.xml → Page → Content.xml`，兼容无入口/厂商变体（本地名匹配，忽略命名空间） |
| 坐标阅读顺序 | TextCode 按 (Y, X) 重排为阅读顺序，同行合并（容差 1.5mm），修复分栏/右起文档顺序错乱 |
| 页码/页眉/页脚剔除 | 页码模式（`— 1 —`/`第X页`/`X/Y`/纯数字）+ 跨页高频重复行（页眉区/页脚区内 ≥50% 页同文本）双重判定 |
| 字体映射错位修复 | 方正书版系 PDF/OFD 把中文标点映射到错误 Unicode 区段（如 `ꎮ`→`。`），内置实测映射表 + 可疑区段扫描 |
| 扫描件判定 | 无文字层页（签章/图片页）计入 `scanned_pages`，供上层接 OCR |

## 快速开始

```python
from ofdkit import extract_text

data = open("doc.ofd", "rb").read()
result = extract_text(data)
print(result.text)  # 清洗后的正文（\n 分隔行）
print(result.pages)  # 页数
print(result.is_scanned)  # 是否整体无文字层
print(result.removed_lines)  # 被剔除的噪音行（审计用）
```

命令行调试：

```bash
python -m ofdkit path/to/doc.ofd        # 打印正文 + 统计
python -m ofdkit doc.ofd --no-clean     # 关闭噪音清洗，看原始行
```

## API

```python
extract_text(data: bytes, *, clean_noise: bool = True,
             fix_glyphs: bool = True) -> ExtractResult
extract_pages(data: bytes) -> list[OFDPage]     # 低层：未清洗的逐页行 + 坐标
```

- `ExtractResult{text, pages, is_scanned, scanned_pages, removed_lines}`
- `OFDPage{index, lines: list[str], line_ys: list[float]}`

格式无关组件（主服务 PDF/DOCX 链路复用，避免双份真相）：

```python
from ofdkit.normalize import normalize_text, FONT_GLYPH_FIXES
from ofdkit.noise import filter_noise_lines, is_page_number
```

## 架构

```
src/ofdkit/
├── model.py       # dataclass：TextRun / OFDPage / ExtractResult
├── reader.py      # ZIP 包导航：入口 → 文档 → 页内容文件
├── textorder.py   # 坐标阅读顺序：Y 分行 → 行内 X 排序
├── noise.py       # 页码/页眉页脚剔除（格式无关，PDF 亦用）
├── normalize.py   # 字体映射错位修复 + 全半角/零宽规范化（格式无关）
└── extract.py     # 主编排：extract_text / extract_pages
```

## 设计约束

1. **零三方依赖**：只用标准库。公文内网离线部署，引入任何依赖都要走镜像审批；
2. **命名空间无关**：各厂商 OFD 变体命名空间前缀不一，统一按 XML 本地名匹配；
3. **不静默丢数据**：可疑区段未命中映射表的字符保留原文并计数上报，交由运维扩充映射表；
4. **坐标容差参数化**：行合并容差、页眉/脚区比例均为参数，适配非 A4 版式。

## 测试

```bash
uv run pytest ofdkit/tests -q     # 库自有测试（含最小 OFD 样本构造器）
```
