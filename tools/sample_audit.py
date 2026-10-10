#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sample_audit.py —— 公文样本抽样分析工具

用途
----
在项目开工前，对历史公文样本做一次体检，回答以下几个决定工期与成本的问题：

  1. 格式分布如何？  docx / pdf / wps / doc / ofd 各占多少
  2. 扫描件占比多少？-> 这是本项目最大的工期变量
  3. 平均页数、平均字数？ -> 用于切片量与存储测算
  4. 按当前样本推算全量 200,000 篇需要多少 OCR 工时

使用方法
--------
    pip install pymupdf python-docx openpyxl

    # 分析整个目录
    python sample_audit.py --path D:/samples

    # 只随机抽样 200 篇
    python sample_audit.py --path D:/samples --sample 200

    # 导出明细 CSV
    python sample_audit.py --path D:/samples --sample 200 --csv audit_result.csv

    # 按全量 20 万篇推算
    python sample_audit.py --path D:/samples --sample 200 --total 200000

判定逻辑说明
------------
PDF 是否有文字层：逐页提取文本，统计有效字符数。
  - 单页字符数 < 阈值(默认 50) 视为"无文字层"（即扫描件或纯图片页）
  - 文档中无文字层页数占比 >= 80%  -> 整篇判定为【扫描件】
  - 占比在 20%~80% 之间             -> 【混合件】（最麻烦：需要按页分别处理）
  - 占比 < 20%                      -> 【文字版】

输出可用性：本脚本只读，不修改任何样本文件。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from tqdm import tqdm
except ImportError:  # tqdm 可选
    def tqdm(iterable, **kwargs):
        return iterable

# OCR 耗时经验值（秒/页），不同硬件差异较大，可按实际情况调整
OCR_SEC_PER_PAGE = 1.5
TEXT_EXTRACT_SEC_PER_DOC = 1.0

SUPPORTED_EXT = {".docx", ".doc", ".pdf", ".wps", ".ofd", ".txt", ".rtf"}


@dataclass
class DocStat:
    """单个文档的体检结果。"""
    path: str
    ext: str
    size_kb: float
    pages: Optional[int] = None
    chars: Optional[int] = None          # 抽取出的正文字符数
    no_text_pages: Optional[int] = None  # 无文字层页数
    kind: str = "unknown"                # text / scanned / hybrid / ok / error
    error: str = ""

    @property
    def needs_ocr(self) -> bool:
        return self.kind in ("scanned", "hybrid")


@dataclass
class AuditConfig:
    sample: Optional[int] = None
    min_chars_per_page: int = 50
    scanned_ratio_threshold: float = 0.8
    hybrid_ratio_threshold: float = 0.2
    total_docs: Optional[int] = None


# ----------------------------------------------------------------------------
# 各类文件的解析
# ----------------------------------------------------------------------------

def _load_pymupdf():
    """兼容新旧两种 API 入口（fitz 入口未来会被移除）。"""
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        import fitz  # type: ignore
        return fitz


def parse_pdf(path: Path, cfg: AuditConfig) -> DocStat:
    fitz = _load_pymupdf()

    stat = DocStat(path=str(path), ext=".pdf", size_kb=path.stat().st_size / 1024)
    doc = fitz.open(str(path))
    try:
        total_pages = len(doc)
        chars = 0
        no_text_pages = 0
        for page in doc:
            text = page.get_text("text") or ""
            c = len(text.strip())
            chars += c
            if c < cfg.min_chars_per_page:
                no_text_pages += 1

        stat.pages = total_pages
        stat.chars = chars
        stat.no_text_pages = no_text_pages

        if total_pages == 0:
            stat.kind = "error"
            stat.error = "PDF 页数为 0"
            return stat

        ratio = no_text_pages / total_pages
        if ratio >= cfg.scanned_ratio_threshold:
            stat.kind = "scanned"
        elif ratio >= cfg.hybrid_ratio_threshold:
            stat.kind = "hybrid"
        else:
            stat.kind = "text"
    finally:
        doc.close()
    return stat


def parse_docx(path: Path, cfg: AuditConfig) -> DocStat:
    import docx  # python-docx

    stat = DocStat(path=str(path), ext=".docx", size_kb=path.stat().st_size / 1024)
    d = docx.Document(str(path))
    body = "\n".join(p.text for p in d.paragraphs)
    table_chars = 0
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                table_chars += len(cell.text)
    stat.chars = len(body) + table_chars
    stat.pages = max(1, round(stat.chars / 1000))  # 粗略估算：约 1000 字/页
    stat.kind = "ok"
    return stat


def parse_txt(path: Path, cfg: AuditConfig) -> DocStat:
    stat = DocStat(path=str(path), ext=path.suffix.lower(),
                   size_kb=path.stat().st_size / 1024)
    for enc in ("utf-8", "gbk", "gb18030", "utf-16"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        stat.kind = "error"
        stat.error = "编码无法识别"
        return stat
    stat.chars = len(text.strip())
    stat.pages = max(1, round(stat.chars / 1000))
    stat.kind = "ok"
    return stat


def parse_doc(path: Path, cfg: AuditConfig) -> DocStat:
    """旧版 .doc 与 .wps 无法直接解析，仅登记格式与体积。"""
    stat = DocStat(path=str(path), ext=path.suffix.lower(),
                   size_kb=path.stat().st_size / 1024)
    stat.kind = "need_convert"
    stat.error = "需转换中间件(LibreOffice/WPS)转为 docx 后处理"
    return stat


def parse_ofd(path: Path, cfg: AuditConfig) -> DocStat:
    stat = DocStat(path=str(path), ext=".ofd",
                   size_kb=path.stat().st_size / 1024)
    stat.kind = "need_convert"
    stat.error = "OFD 需专用解析器(ofdrw 等)"
    return stat


PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".doc": parse_doc,
    ".wps": parse_doc,
    ".ofd": parse_ofd,
    ".txt": parse_txt,
    ".rtf": parse_doc,
}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def _isatty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def collect_files(root: Path) -> list[Path]:
    files = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.startswith("~$"):  # Office 临时文件
            continue
        if p.suffix.lower() in SUPPORTED_EXT:
            files.append(p)
    return files


def audit(files: list[Path], cfg: AuditConfig) -> list[DocStat]:
    stats: list[DocStat] = []
    missing_lib: set[str] = set()

    for f in tqdm(files, desc="分析样本", unit="doc",
                  disable=not _isatty()):
        ext = f.suffix.lower()
        parser = PARSERS.get(ext)
        if parser is None:
            stats.append(DocStat(path=str(f), ext=ext,
                                 size_kb=f.stat().st_size / 1024,
                                 kind="unsupported", error="不支持的格式"))
            continue
        try:
            stats.append(parser(f, cfg))
        except ImportError as e:
            missing_lib.add(str(e))
            stats.append(DocStat(path=str(f), ext=ext,
                                 size_kb=f.stat().st_size / 1024,
                                 kind="error", error=f"缺少依赖: {e}"))
        except Exception as e:  # noqa: BLE001
            stats.append(DocStat(path=str(f), ext=ext,
                                 size_kb=f.stat().st_size / 1024,
                                 kind="error", error=f"{type(e).__name__}: {e}"))

    if missing_lib:
        print("\n[!] 缺少依赖库，请先安装：")
        for m in missing_lib:
            print(f"    {m}")
        print("    pip install pymupdf python-docx openpyxl tqdm\n")
    return stats


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "-"


def report(stats: list[DocStat], cfg: AuditConfig, csv_out: Optional[str]) -> None:
    n = len(stats)
    if n == 0:
        print("未找到可分析的文档。请检查路径与文件格式。")
        return

    print("\n" + "=" * 68)
    print(f"公文样本体检报告 —— 样本数 {n}")
    print("=" * 68)

    # 1. 格式分布
    print("\n[1] 格式分布")
    ext_cnt = Counter(s.ext for s in stats)
    for ext, c in ext_cnt.most_common():
        print(f"    {ext:<8} {c:>6} 篇   {pct(c, n)}")

    # 2. 扫描件判定（仅对 PDF 有效）
    pdfs = [s for s in stats if s.ext == ".pdf" and s.kind in ("text", "scanned", "hybrid")]
    print("\n[2] PDF 文字层情况  ★ 决定工期的关键指标")
    if pdfs:
        k_cnt = Counter(s.kind for s in pdfs)
        for k in ("text", "hybrid", "scanned"):
            if k in k_cnt:
                label = {"text": "有文字层", "hybrid": "混合件(部分页需OCR)",
                         "scanned": "扫描件(需全文OCR)"}[k]
                print(f"    {label:<24} {k_cnt[k]:>6} 篇   {pct(k_cnt[k], len(pdfs))}")

        scan_like = k_cnt.get("scanned", 0) + k_cnt.get("hybrid", 0)
        ratio = scan_like / len(pdfs)
        print(f"\n    >> 需要 OCR 的 PDF 占比： {pct(scan_like, len(pdfs))}  ({scan_like}/{len(pdfs)})")

        total_pages = sum(s.pages or 0 for s in pdfs)
        no_text = sum(s.no_text_pages or 0 for s in pdfs)
        print(f"    >> PDF 总页数：{total_pages}，其中无文字层 {no_text} 页 ({pct(no_text, total_pages)})")
    else:
        print("    样本中无有效 PDF")

    # 3. 规模指标
    print("\n[3] 篇幅与体量")
    chars_list = [s.chars for s in stats if s.chars]
    pages_list = [s.pages for s in stats if s.pages]
    sizes = [s.size_kb for s in stats]

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    if chars_list:
        print(f"    平均字数：{avg(chars_list):,.0f} 字    "
              f"(中位数 {sorted(chars_list)[len(chars_list)//2]:,} 字)")
    if pages_list:
        print(f"    平均页数：{avg(pages_list):.1f} 页")
    print(f"    平均文件：{avg(sizes):,.0f} KB")
    mb_total = sum(sizes) / 1024
    if mb_total < 1:
        print(f"    样本总体量：{sum(sizes):,.0f} KB")
    else:
        print(f"    样本总体量：{mb_total:,.2f} MB")

    # 4. 错误与需转换
    err = [s for s in stats if s.kind == "error"]
    conv = [s for s in stats if s.kind == "need_convert"]
    if conv:
        print(f"\n[4] 需先转换格式：{len(conv)} 篇 ({pct(len(conv), n)})")
        for e, c in Counter(s.ext for s in conv).most_common():
            print(f"    {e:<8} {c:>6} 篇")
    if err:
        print(f"\n[5] 解析失败：{len(err)} 篇 ({pct(len(err), n)})")
        for e in err[:5]:
            print(f"    {Path(e.path).name}  ->  {e.error}")

    # 5. 全量推算
    if cfg.total_docs:
        print("\n" + "=" * 68)
        print(f"按全量 {cfg.total_docs:,} 篇推算")
        print("=" * 68)

        if pdfs:
            scan_pages = (sum(s.no_text_pages or 0 for s in pdfs) / len(pdfs)) * cfg.total_docs
            ocr_hours = scan_pages * OCR_SEC_PER_PAGE / 3600
            print(f"    需 OCR 页数：  {scan_pages:,.0f} 页")
            print(f"    OCR 耗时：     {ocr_hours:,.0f} 小时  "
                  f"(单进程 @{OCR_SEC_PER_PAGE}s/页)")
            print(f"                   -> 8 进程并行约 {ocr_hours/8:,.0f} 小时"
                  f" / 按每天 12h 计约 {ocr_hours/8/12:,.1f} 天")
            print("    注：此为串行估算，实际受显卡型号、OCR 引擎影响较大")

        if chars_list:
            total_chars = avg(chars_list) * cfg.total_docs
            chunks = total_chars / (400 - 80)  # 切片 400 字 / overlap 80
            print(f"\n    全量总字数：   {total_chars/1e8:,.2f} 亿字")
            print(f"    预估切片数：   {chunks:,.0f} 个")
            print(f"    向量存储：     {chunks*1024*4/1e9:,.1f} GB (1024维 fp32，未含索引开销)")
            print(f"    Embedding 耗时：{chunks/200/3600:,.1f} 小时 (按 200 chunks/s)")

        total_size = avg(sizes) * cfg.total_docs / 1024 / 1024
        print(f"\n    原文存储：     {total_size:,.1f} GB")

    print()

    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["文件路径", "格式", "大小KB", "页数", "字数",
                        "无文字层页数", "判定", "错误"])
            for s in stats:
                w.writerow([s.path, s.ext, f"{s.size_kb:.0f}", s.pages, s.chars,
                            s.no_text_pages, s.kind, s.error])
        print(f"明细已导出：{csv_out}\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="公文样本抽样分析工具 —— 统计格式分布、扫描件占比、篇幅与全量推算")
    ap.add_argument("--path", required=True, help="样本目录（会递归扫描）")
    ap.add_argument("--sample", type=int, default=None,
                    help="随机抽样篇数（建议 200），不填则分析全部")
    ap.add_argument("--total", type=int, default=None,
                    help="全量文档总数，用于推算（如 200000）")
    ap.add_argument("--csv", default=None, help="导出明细 CSV 路径")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，默认 42 保证可复现")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"路径不存在或不是目录：{root}")
        return 1

    cfg = AuditConfig(sample=args.sample, total_docs=args.total)

    files = collect_files(root)
    if not files:
        print(f"未在 {root} 下找到支持的文档。支持格式：{', '.join(sorted(SUPPORTED_EXT))}")
        return 1

    print(f"扫描到 {len(files)} 个文档")
    if args.sample and args.sample < len(files):
        random.seed(args.seed)
        files = random.sample(sorted(files), args.sample)
        print(f"随机抽样 {args.sample} 篇（seed={args.seed}，可复现）")

    stats = audit(files, cfg)
    report(stats, cfg, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
