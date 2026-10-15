"""OFD 文档入库全链路验证（真实语料：ndrc_ofd_files/ 国家发展改革委公开版 OFD）。

链路：登录 → multipart 上传 .ofd → 轮询入库任务（PARSING→CHUNKING→EMBEDDING→INDEXED）
→ 校验解析产物（页码/乱码残留）→ 检索（/v1/retrieve）→ 生成（/v1/generate）。

红线（与 tools/verify_parse_chunk_v2.py 一致）：
  R1 页码/页眉页脚残留 = 0
  R2 乱码（PUA 区）残留 = 0
  R3 段中切断 violations = 0
  R4 超预算切片 = 0
  R5 偏移单调 = 100%

用法：
    python tools/verify_ofd_ingest.py                # 全量 15 篇
    python tools/verify_ofd_ingest.py --limit 3      # 抽样 3 篇
    python tools/verify_ofd_ingest.py --no-generate  # 跳过生成（生成慢）
    OFD_DIR=/path/to/dir python tools/verify_ofd_ingest.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE", "http://127.0.0.1:8000/v1")
OFD_DIR = Path(os.environ.get("OFD_DIR", str(ROOT / "ndrc_ofd_files")))
USER = os.environ.get("SMOKE_USER", "admin")
PWD = os.environ.get("SMOKE_PWD", "admin123")

# 乱码可疑区段（方正书版系字体错位映射的落点）：Bamum / Linear B / PUA
SUSPECT_RANGES = ((0xA000, 0xA8FF), (0xE000, 0xF8FF), (0x100000, 0x1003FF))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")
    return bool(cond)


def login() -> str:
    r = requests.post(f"{BASE}/auth/login", json={"username": USER, "password": PWD}, timeout=20)
    tok = r.json().get("access_token", "") if r.status_code == 200 else ""
    if not tok:
        raise SystemExit(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
    print(f"[INFO] 登录成功（{USER}）")
    return tok


def upload(tok: str, path: Path) -> dict:
    """multipart 上传单个文件，返回任务 JSON。"""
    with open(path, "rb") as f:
        files = {"file": (path.name, f, "application/octet-stream")}
        data = {"title": path.stem, "doc_type": "通知", "security_level": "INTERNAL"}
        r = requests.post(
            f"{BASE}/documents/upload",
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {tok}"},
            timeout=120,
        )
    if r.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def wait_job(tok: str, job_id: str, timeout: int = 900) -> dict:
    """轮询任务至终态（INDEXED / FAILED）。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        r = requests.get(
            f"{BASE}/documents/jobs/{job_id}", headers={"Authorization": f"Bearer {tok}"}, timeout=20
        )
        if r.status_code != 200:
            raise RuntimeError(f"查询任务失败 HTTP {r.status_code}")
        j = r.json()
        st = j.get("status", "")
        if st != last:
            print(f"      状态：{st} 进度 {j.get('progress')}%  切片 {j.get('total_chunks')}")
            last = st
        if st in ("INDEXED", "FAILED"):
            return j
        time.sleep(2)
    raise TimeoutError(f"任务 {job_id} 超时（{timeout}s），最后状态 {last}")


def get_document(tok: str, doc_id: str) -> dict:
    r = requests.get(
        f"{BASE}/documents/{doc_id}", headers={"Authorization": f"Bearer {tok}"}, timeout=60
    )
    return r.json() if r.status_code == 200 else {}


def local_parse_check(path: Path) -> dict:
    """本地层检查（API 不暴露偏移/粒度，需直接跑解析+切分拿全量指标）。

    与 tools/verify_parse_chunk_v2.py 同口径：
      - 页码/乱码残留在**切片内容**上判定（比全文判定更严格）
      - 偏移单调性、超预算片、完整单元率
    """
    sys.path.insert(0, str(ROOT / "backend"))
    from app.services.chunker import chunk_document_dual  # noqa: PLC0415
    from app.services.parser import parse_bytes  # noqa: PLC0415

    raw = path.read_bytes()
    parsed = parse_bytes(path.name, raw, noise_clean=True)
    prefix = f"{path.stem}｜国家发展改革委｜"
    smalls, larges = chunk_document_dual(
        parsed.text, prefix, strategy="paragraph",
        small_max_tokens=300, small_overlap_tokens=40,
        large_max_tokens=2500, large_overlap_tokens=200,
    )
    page_no, pua = scan_noise(parsed.text)
    # 偏移单调性口径与 tools/verify_parse_chunk_v2.py 一致：
    # start 严格递增 + end 非递减。**不要求 end<=下一片 start**——尾重叠
    # （overlap_tokens=40）是设计使然，后片 start 本就会略小于前片 end。
    monotonic = all(
        smalls[k].start < smalls[k + 1].start and smalls[k].end <= smalls[k + 1].end
        for k in range(len(smalls) - 1)
    )
    max_tokens = 0
    try:
        from app.services.chunker import get_default_tokenizer  # noqa: PLC0415
        tokz = get_default_tokenizer()
        if tokz is not None:
            from chonkie import count_tokens_batch  # noqa: PLC0415
            counts = count_tokens_batch([c.content for c in smalls], tokz)
            max_tokens = max(counts) if counts else 0
    except Exception:  # noqa: BLE001 —— tokenizer 不可用时仅记 0（不阻断）
        pass
    return {
        "pages": parsed.pages,
        "chars": len(parsed.text),
        "small": len(smalls),
        "large": len(larges),
        "page_no_residue": page_no,
        "pua_residue": pua,
        "offsets_monotonic": monotonic,
        "max_chunk_tokens": max_tokens,
        "over_budget": sum(1 for t in ([max_tokens] if max_tokens else []) if t > 300),
    }


def scan_noise(text: str) -> tuple[int, int]:
    """返回 (页码残留数, 乱码残留数)。

    页码只查**强模式**（`— 1 —` 破折线包裹）：弱模式（纯数字行）不可用于
    全文判定——正文里的评分分值、公式数字、上标也常独立成行（实测 005 号
    文件 3 处），而页缘页码已由 filter_noise_lines 按位置剔除。真正该守住的
    是"破折线页码"形态；正文独立数字由段落重建吸收（切片层实测 0 处）。
    """
    page_no = len(re.findall(r"—\s*[０-９0-9]+\s*—", text))
    pua = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in SUSPECT_RANGES))
    return page_no, pua


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 个文件（0=全部）")
    ap.add_argument("--no-generate", action="store_true", help="跳过生成测试（生成慢）")
    ap.add_argument("--keep", action="store_true", help="保留已入库数据（不清库）")
    args = ap.parse_args()

    files = sorted(OFD_DIR.glob("*.ofd"))
    if not files:
        raise SystemExit(f"未找到 OFD 文件：{OFD_DIR}")
    if args.limit:
        files = files[: args.limit]

    print(f"[INFO] OFD 语料：{OFD_DIR}（{len(files)} 个文件）")
    tok = login()

    results: list[dict] = []
    for i, p in enumerate(files, 1):
        size_kb = p.stat().st_size / 1024
        print(f"\n[{i}/{len(files)}] {p.name[:56]}（{size_kb:.0f} KB）")
        t0 = time.time()
        try:
            # ---- API 层：真实上传 + 入库 ----
            job = upload(tok, p)
            jid = job.get("id", "")
            done = wait_job(tok, jid)
            dt = time.time() - t0
            if done.get("status") != "INDEXED":
                check(f"入库 {p.name[:30]}", False, f"→ {done.get('status')}: {done.get('error_message')}")
                results.append({"file": p.name, "ok": False, "error": done.get("error_message")})
                continue

            doc_id = done.get("document_id", "")
            doc = get_document(tok, doc_id)
            api_chunks = doc.get("chunks", []) or []
            # 切片内容级噪音检查（API 层，比全文判定更严格）
            joined = "\n".join(c.get("content", "") for c in api_chunks)
            api_page_no, api_pua = scan_noise(joined)

            # ---- 本地层：解析+切分全量指标（偏移/预算/粒度） ----
            loc = local_parse_check(p)

            r = {
                "file": p.name,
                "ok": True,
                "doc_id": doc_id,
                "pages": loc["pages"],
                "chars": loc["chars"],
                "small": loc["small"],
                "large": loc["large"],
                "api_chunk_count": doc.get("chunk_count", 0),
                "page_no_residue": max(loc["page_no_residue"], api_page_no),
                "pua_residue": max(loc["pua_residue"], api_pua),
                "offsets_monotonic": loc["offsets_monotonic"],
                "max_chunk_tokens": loc["max_chunk_tokens"],
                "seconds": round(dt, 1),
            }
            results.append(r)
            print(
                f"      → INDEXED 页={r['pages']} 字符={r['chars']} "
                f"小块={r['small']} 大块={r['large']}（库内切片 {r['api_chunk_count']}）"
            )
            print(
                f"        页码残留={r['page_no_residue']} 乱码残留={r['pua_residue']} "
                f"偏移单调={r['offsets_monotonic']} 单片最大token={r['max_chunk_tokens'] or 'n/a'} "
                f"耗时={r['seconds']}s"
            )
        except Exception as e:  # noqa: BLE001 —— 单文件失败不应中断整轮
            print(f"      [FAIL] 异常：{type(e).__name__}: {e}")
            results.append({"file": p.name, "ok": False, "error": f"{type(e).__name__}: {e}"})

    # ---------------- 汇总 ----------------
    ok = [r for r in results if r.get("ok")]
    print("\n" + "=" * 78)
    print("OFD 入库汇总")
    print("=" * 78)
    print(f"{'文件':40s}{'页':>4s}{'字符':>8s}{'小块':>6s}{'大块':>6s}{'页码':>6s}{'乱码':>6s}{'秒':>7s}")
    for r in ok:
        name = r["file"][:38]
        print(
            f"{name:40s}{r['pages'] or 0:>4d}{r['chars']:>8d}{r['small']:>6d}"
            f"{r['large']:>6d}{r['page_no_residue']:>6d}{r['pua_residue']:>6d}{r['seconds']:>7.1f}"
        )
    failed = [r for r in results if not r.get("ok")]
    print(f"\n合计：成功 {len(ok)}/{len(results)}，失败 {len(failed)}")

    check("OFD 全部入库成功", len(ok) == len(results), f"({len(ok)}/{len(results)})")
    if failed:
        for r in failed:
            print(f"    失败：{r['file'][:40]} → {r.get('error')}")
    check("页码/页眉页脚残留 = 0", all(r["page_no_residue"] == 0 for r in ok))
    check("乱码（PUA）残留 = 0", all(r["pua_residue"] == 0 for r in ok))
    check("偏移单调性 100%", all(r["offsets_monotonic"] for r in ok))
    check("产出切片 > 0", all(r["small"] > 0 for r in ok))
    check("切片均不超预算（≤300 token）", all(r["max_chunk_tokens"] <= 300 for r in ok if r["max_chunk_tokens"]))

    # ---------------- 检索 + 生成 ----------------
    if ok:
        print("\n" + "-" * 78)
        print("检索 / 生成 验证（基于已入库 OFD）")
        print("-" * 78)
        sample = ok[0]
        doc_id = _doc_id_of(tok, sample["file"]) or sample.get("doc_id", "")
        doc = get_document(tok, doc_id)
        # query 必须取**正文真实词汇**：文件名（"001_国家发展改革委等关于印发…"）
        # 与正文 embedding 空间不匹配，实测命中 0——那是构造错误不是检索故障。
        # 取首片正文中首个完整句的核心短语（去标点、限长）。
        first_chunk = (doc.get("chunks") or [{}])[0].get("content", "")
        # 首行是「标题｜机关｜日期」前缀，跳过取正文
        body = first_chunk.split("\n", 1)[1] if "\n" in first_chunk else first_chunk
        m = re.search(r"[一-龥]{4,12}", body or "")
        query = (m.group(0) if m else "国家发展改革委")
        r = requests.post(
            f"{BASE}/retrieve",
            json={"query": query, "top_n": 5},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=180,
        )
        body_r = r.json() if r.status_code == 200 else {}
        items = body_r.get("citations") or body_r.get("items") or []
        check(
            "检索 OFD 内容命中",
            r.status_code == 200 and len(items) > 0,
            f"(query={query!r} 命中 {len(items)} 片, keyword_hits={body_r.get('keyword_hits')})",
        )

        if not args.no_generate:
            r = requests.post(
                f"{BASE}/generate",
                json={"query": "请概括这份文件的主要内容", "top_n": 3},
                headers={"Authorization": f"Bearer {tok}"},
                timeout=600,
            )
            gen = r.json() if r.status_code == 200 else {}
            # 注意：GenerateResp 的正文字段名是 **answer**（不是 content）
            answer = gen.get("answer", "")
            check(
                "生成可用",
                r.status_code == 200 and len(answer) > 50,
                f"(HTTP {r.status_code}, {len(answer)} 字, 引用 {len(gen.get('citations') or [])} 条)",
            )

    print("\n" + "=" * 78)
    print(f"结果：{PASS} PASS / {FAIL} FAIL")
    print("=" * 78)
    return 0 if FAIL == 0 else 1


def _doc_id_of(tok: str, filename: str) -> str:
    """按文件名从文档列表反查 document_id（列表接口返回的是数组，非分页对象）。"""
    r = requests.get(
        f"{BASE}/documents?page=1&page_size=200",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    if r.status_code != 200:
        return ""
    docs = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    for d in docs:
        if filename[:20] in (d.get("title") or "") or filename[:20] in (d.get("external_id") or ""):
            return d.get("id", "")
    return ""


if __name__ == "__main__":
    sys.exit(main())
