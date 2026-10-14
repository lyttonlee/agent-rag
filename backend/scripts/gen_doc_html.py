"""把 docs/ 下的入库流程说明 Markdown 渲染为浅色科技风 HTML 阅读版。

用法：PYTHONPATH=. uv run python scripts/gen_doc_html.py
输入：docs/企业公文知识库_文档入库流程说明.md
输出：reports/企业公文知识库_文档入库流程说明.html

样式与 2026-10-14 全链路验证报告保持一致（浅色主题、科技感、内容铺满视口）。
"""

from __future__ import annotations

import re
from pathlib import Path

import markdown

DOC = Path(__file__).resolve().parents[2] / "docs" / "企业公文知识库_文档入库流程说明.md"
OUT = Path(__file__).resolve().parents[2] / "reports" / "企业公文知识库_文档入库流程说明.html"

CSS = """
:root {
  --bg: #f5f7fa; --panel: #ffffff; --border: #e4e9f0; --text: #1a2233;
  --muted: #5b6679; --accent: #1668dc; --accent-soft: #e8f1ff;
  --green: #0f8a4d; --red: #c0392b; --amber: #b7791f;
  --code-bg: #f0f3f8; --mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 40px 28px 80px; }
header.hero {
  background: linear-gradient(135deg, #0b1f3a 0%, #123a6b 55%, #1668dc 100%);
  border-radius: 14px; padding: 34px 38px; color: #fff; margin-bottom: 28px;
}
header.hero h1 { margin: 0 0 8px; font-size: 26px; letter-spacing: .5px; }
header.hero p { margin: 0; color: #c9dcf7; font-size: 13.5px; }
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 26px 30px; margin-bottom: 22px;
  box-shadow: 0 1px 3px rgba(16, 34, 61, .05);
}
h2 {
  font-size: 20px; margin: 34px 0 14px; padding-left: 12px;
  border-left: 4px solid var(--accent); color: #0b2a52;
}
h3 { font-size: 16.5px; margin: 22px 0 10px; color: #123a6b; }
h4 { font-size: 15px; margin: 16px 0 8px; color: #2b4a73; }
p { margin: 8px 0; }
ul, ol { margin: 8px 0; padding-left: 24px; }
li { margin: 4px 0; }
code {
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 5px;
  padding: 1px 6px; font-family: var(--mono); font-size: 13px; color: #b03a5b;
}
pre {
  background: #0d1b2e; color: #d7e6ff; border-radius: 10px; padding: 18px 20px;
  overflow-x: auto; font-family: var(--mono); font-size: 12.8px; line-height: 1.65;
}
pre code { background: none; border: none; color: inherit; padding: 0; }
table {
  width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 13.8px;
  background: #fff;
}
th {
  background: var(--accent-soft); color: #0b2a52; text-align: left;
  padding: 9px 12px; border: 1px solid var(--border); font-weight: 600;
}
td { padding: 8px 12px; border: 1px solid var(--border); vertical-align: top; }
tr:nth-child(even) td { background: #fafbfe; }
blockquote {
  margin: 14px 0; padding: 10px 18px; border-left: 4px solid var(--amber);
  background: #fff9ec; border-radius: 0 8px 8px 0; color: #6b4d16;
}
hr { border: none; border-top: 1px solid var(--border); margin: 30px 0; }
strong { color: #0b2a52; }
a { color: var(--accent); }
"""


def md_to_html(md_text: str) -> str:
    """Markdown → HTML（表格/围栏代码/目录等扩展全开）。"""
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"title": "目录"}},
    )


def main() -> None:
    md_text = DOC.read_text(encoding="utf-8")
    body = md_to_html(md_text)
    # 文档头（标题+副标题）从 md 首块提取做 hero，其余进 panel
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>企业公文知识库 · 文档入库流程说明</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <h1>企业公文知识库 · 文档入库流程说明</h1>
  <p>上传 → 解析 → 切分 → 向量化 → 入库 → 检索 → 生成 ｜ 重点：切分与检索（方案 / 选型理由 / 优点 / 缺点）｜ V1.0 · 2026-10-14</p>
</header>
<div class="panel">
{body}
</div>
</div>
</body>
</html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    # 自校验：无未转换的 md 残留语法、关键章节在位
    leftovers = re.findall(r"^\s*#{1,4}\s", html, flags=re.M)
    for kw in ("切分（重点）", "检索（重点）", "为什么这么选择", "缺点与权衡", "附录 A"):
        assert kw in html, f"missing section: {kw}"
    assert not leftovers, "markdown headings left unconverted"
    print(f"OK -> {OUT} ({len(html)} chars)")


if __name__ == "__main__":
    main()
