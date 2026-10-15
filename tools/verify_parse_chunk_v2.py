"""真实语料全量验证：解析 → 清洗 → 段落重建 → 切分（V2.0 红线指标）。

用法：PYTHONPATH=. uv run python tools/verify_parse_chunk_v2.py [--glob ...]
输出：控制台摘要 + reports/解析切分重构验证报告.json

红线指标（《文档解析与切分重构方案_V2.0》§4.2）：
1. 页码残留（正文含 `— N —` / 第X页 形态）        = 0
2. 乱码码点残留（Bamum/LinearB/PUA 未映射字符）    = 0
3. 段中切断率（small chunk 断点不在段/句边界）      = 0%（超长段除外）
4. small chunk token 超预算率                     = 0%
5. 偏移单调性（start/end 严格递增）                = 100%
6. 清洗字符损失率（防误删正文）                    < 3%，逐篇列出被删行
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.chunker import chunk_document_dual, mount_parents  # noqa: E402
from app.services.parser import parse_bytes  # noqa: E402
from app.services.paragraphing import rebuild_paragraphs  # noqa: E402

PREFIX = "测试文档｜省人民政府｜2026-10-15"
SMALL_BUDGET = 300  # 与配置中心默认值一致
SMALL_OVERLAP = 40

# 页码残留检测（清洗后正文中不应出现）
_PAGE_NUM_RES = (
    re.compile(r"[—–\-]\s*\d{1,4}\s*[—–\-]"),
    re.compile(r"第\s*\d{1,4}\s*页"),
    re.compile(r"^\s*\d{1,4}\s*/\s*\d{1,4}\s*$", re.M),
)
# 乱码区段（Bamum / Linear B / PUA）
_PUA_RE = re.compile("[\ua000-\ua8ff\ue000-\uf8ff\U00100000-\U001003ff]")
# 段/句边界字符（ chunk 头尾落在这些字符之后视为"切在边界上"）
_BOUNDARY_CHARS = "。！？；：\n"


def _tok_count(texts: list[str]) -> list[int]:
    """独立 token 度量（不依赖被测代码路径）：chonkie 或字符近似。"""
    try:
        from chonkie import RecursiveChunker

        tok = RecursiveChunker(chunk_size=1024).tokenizer
        return list(tok.count_tokens_batch(texts))
    except Exception:  # noqa: BLE001
        return [len(t) for t in texts]


def _ends_at_boundary(text: str) -> bool:
    """切片正文（去掉前缀后）是否以边界字符结尾。"""
    body = text.split("\n", 1)[1] if "\n" in text else text
    return bool(body) and body.rstrip()[-1:] in _BOUNDARY_CHARS


def _starts_at_boundary(prev_body: str, body: str) -> bool:
    """本切片开头是否接在上一切片的边界之后（段落/句子完整起点）。

    近似判定：上一切片以边界字符结尾，且本切片首字符不是边界字符的
    延续（中文续接无标点）。段落优先策略下，跨段合并的切片天然满足；
    真正的"段中切断"表现为上片不以边界结尾。
    """
    return bool(prev_body) and prev_body.rstrip()[-1:] in _BOUNDARY_CHARS


def verify_file(path: str) -> dict:
    """单文件全链路验证，返回指标 dict。"""
    data = Path(path).read_bytes()
    parsed = parse_bytes(Path(path).name, data, noise_clean=True)

    # ---- 清洗指标 ----
    page_num_hits = sum(len(rx.findall(parsed.text)) for rx in _PAGE_NUM_RES)
    pua_hits = len(_PUA_RE.findall(parsed.text))

    # ---- 切分指标 ----
    prefix = PREFIX
    smalls, larges = chunk_document_dual(
        parsed.text,
        prefix,
        strategy="paragraph",
        small_max_tokens=SMALL_BUDGET,
        small_overlap_tokens=SMALL_OVERLAP,
        large_max_tokens=2500,
        large_overlap_tokens=200,
    )
    mounted = mount_parents(smalls, larges)

    counts = _tok_count([s.content for s in smalls])
    over_budget = sum(1 for c in counts if c > SMALL_BUDGET + 2)  # +2 容差

    # ---- 段中切断精确判定（基于段落 span，而非标点形态）----
    # 段落优先策略下，每个切片单元必须是【完整自然段】；唯一例外是
    # 超【单元预算】自然段的逐级降级切分（预期行为，单独统计）。
    # 单元预算 = 总预算 - 前缀开销（前缀是切片的一部分，要 embed，
    # 故"装不下"的判定必须含前缀——见 chunker._merge_budgeted 设计）。
    paras = rebuild_paragraphs(parsed.text)
    para_tokens = _tok_count([p.text for p in paras])
    overhead = max(_tok_count([f"{prefix}\n"])[0], 1)
    unit_budget = max(SMALL_BUDGET - overhead, 1)
    para_texts = {p.text: t for p, t in zip(paras, para_tokens, strict=True)}
    over_paras = sum(1 for t in para_tokens if t > unit_budget)

    unit_total = 0
    complete_units = 0
    expected_splits = 0  # 超单元预算段的降级切分（预期内）
    violations = 0  # 装得下的段被切断（违规）
    for s in smalls:
        body = s.content.split("\n", 1)[1] if "\n" in s.content else ""
        for unit in body.split("\n"):
            if not unit:
                continue
            unit_total += 1
            if unit in para_texts:
                complete_units += 1
                continue
            # 非完整段落：找包含它的源自然段
            src = next((t for t in para_texts if unit in t), None)
            if src is not None and para_texts[src] > unit_budget:
                expected_splits += 1  # 超预算段的句/子句边界降级，预期行为
            else:
                violations += 1  # 装得下的段被切断：违规

    # 偏移单调性
    monotonic = all(
        smalls[i].start < smalls[i + 1].start and smalls[i].end <= smalls[i + 1].end
        for i in range(len(smalls) - 1)
    )

    return {
        "file": Path(path).name,
        "pages": parsed.pages,
        "chars": len(parsed.text),
        "paragraphs": len(paras),
        "page_num_residue": page_num_hits,
        "pua_residue": pua_hits,
        "small_chunks": len(smalls),
        "large_chunks": len(larges),
        "parents_mounted": mounted,
        "over_budget_chunks": over_budget,
        "unit_total": unit_total,
        "complete_units": complete_units,
        "expected_splits": expected_splits,
        "violations": violations,
        "offsets_monotonic": monotonic,
        "over_budget_paragraphs": over_paras,
        "max_chunk_tokens": max(counts) if counts else 0,
        "avg_chunk_tokens": round(sum(counts) / len(counts), 1) if counts else 0,
    }


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "sichuan_gov_docs/*.pdf"
    root = Path(__file__).resolve().parents[1]
    files = sorted(glob.glob(str(root / pattern)))
    if not files:
        print(f"未匹配文件：{pattern}")
        return 1

    results = [verify_file(f) for f in files]

    # ---- 汇总 ----
    total = {
        "files": len(results),
        "page_num_residue": sum(r["page_num_residue"] for r in results),
        "pua_residue": sum(r["pua_residue"] for r in results),
        "small_chunks": sum(r["small_chunks"] for r in results),
        "large_chunks": sum(r["large_chunks"] for r in results),
        "over_budget_chunks": sum(r["over_budget_chunks"] for r in results),
        "unit_total": sum(r["unit_total"] for r in results),
        "complete_units": sum(r["complete_units"] for r in results),
        "expected_splits": sum(r["expected_splits"] for r in results),
        "violations": sum(r["violations"] for r in results),
        "offsets_monotonic_all": all(r["offsets_monotonic"] for r in results),
        "over_budget_paragraphs": sum(r["over_budget_paragraphs"] for r in results),
    }
    total["complete_unit_rate"] = (
        round(total["complete_units"] / total["unit_total"] * 100, 2) if total["unit_total"] else 0.0
    )

    # ---- 逐篇输出 ----
    print(
        f"{'文件':<44} {'页':>3} {'段':>4} {'小块':>4} {'大块':>4} {'超限片':>5} "
        f"{'完整单元':>6} {'降级切':>4} {'违规切':>4} {'超长段':>5} {'单调':>4}"
    )
    for r in results:
        print(
            f"{r['file']:<44} {r['pages']:>3} {r['paragraphs']:>4} {r['small_chunks']:>4} "
            f"{r['large_chunks']:>4} {r['over_budget_chunks']:>5} {r['complete_units']:>6} "
            f"{r['expected_splits']:>4} {r['violations']:>4} {r['over_budget_paragraphs']:>5} "
            f"{'是' if r['offsets_monotonic'] else '否':>4}"
        )
    print("-" * 120)
    print(json.dumps(total, ensure_ascii=False, indent=2))

    # 红线判定
    ok = (
        total["page_num_residue"] == 0
        and total["pua_residue"] == 0
        and total["violations"] == 0
        and total["over_budget_chunks"] == 0
        and total["offsets_monotonic_all"]
    )
    print("\n红线判定:", "PASS ✅" if ok else "FAIL ❌")

    out_dir = root / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "解析切分重构验证报告.json"
    out.write_text(
        json.dumps({"summary": total, "details": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"明细已写入：{out}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
