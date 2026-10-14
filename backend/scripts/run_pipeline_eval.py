"""全链路评测：上传→解析→切分→入库→检索→生成，采集性能与准确率指标。

用法：uv run python scripts/run_pipeline_eval.py
- 复用 admin JWT；以真实政务公文《健全基本医疗保险参保长效机制实施意见》为测试文档；
- 采集：上传/入库端到端耗时、切片数、原文是否落 S3、检索延迟与命中、生成延迟与引用；
- 准确率：以"大学生在学籍地参加居民医保可按学制趸缴医保费"为目标相关段（特征词"趸缴"），
  计算检索 precision@k / top1 是否命中；
- 指标写入 scripts/_pipeline_metrics.json。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
PDF = Path(__file__).resolve().parent.parent.parent / "sichuan_gov_docs" / "012_川办发32号删减版.pdf"
OUT = Path(__file__).resolve().parent / "_pipeline_metrics.json"

RETRIEVE_QUERY = "大学生参加居民医保如何缴费"
GT_MARKER = "趸缴"  # 目标相关段的特征词
GEN_QUERY = "请依据基本医保参保长效机制实施意见，就落实大学生参保缴费政策起草一份工作通知"


def login() -> str:
    r = requests.post(
        f"{BASE}/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def poll_job(tok: str, job_id: str, timeout: int = 120) -> dict:
    t0 = time.perf_counter()
    last = {}
    while time.perf_counter() - t0 < timeout:
        r = requests.get(
            f"{BASE}/v1/documents/jobs/{job_id}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=15,
        )
        r.raise_for_status()
        last = r.json()
        if last["status"] in ("INDEXED", "FAILED"):
            break
        time.sleep(1.5)
    return last


def main() -> None:
    metrics: dict = {"doc": {"file": PDF.name, "title": "健全基本医疗保险参保长效机制实施意见"}}
    tok = login()

    # 0) 基线文档数
    stats = requests.get(f"{BASE}/v1/documents/stats", headers=auth(tok), timeout=15).json()
    metrics["baseline_documents"] = stats["total_documents"]
    metrics["baseline_chunks"] = stats["total_chunks"]

    # 1) 上传（解析/切分/向量化/入库 后台状态机）
    with open(PDF, "rb") as f:
        t0 = time.perf_counter()
        r = requests.post(
            f"{BASE}/v1/documents/upload",
            headers={"Authorization": f"Bearer {tok}"},
            files={"file": (PDF.name, f, "application/pdf")},
            data={
                "title": "四川省人民政府办公厅关于健全基本医疗保险参保长效机制的实施意见",
                "doc_type": "意见",
                "doc_no": "川办发〔2025〕32号",
                "issuer": "四川省人民政府办公厅",
                "security_level": "INTERNAL",
                "chunk_strategy": "paragraph",
                "chunk_size": "200",
                "chunk_overlap": "30",
            },
            timeout=30,
        )
        upload_ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    job = r.json()
    metrics["upload"] = {"api_latency_ms": round(upload_ms, 1), "job_id": job["id"], "initial_status": job["status"]}

    # 2) 轮询至 INDEXED
    t0 = time.perf_counter()
    final = poll_job(tok, job["id"])
    ingest_ms = (time.perf_counter() - t0) * 1000
    metrics["ingest"] = {
        "status": final.get("status"),
        "total_ms": round(ingest_ms, 1),
        "total_chunks": final.get("total_chunks"),
        "document_id": final.get("document_id"),
        "error": final.get("error_message"),
    }
    doc_id = final.get("document_id")

    # 3) 原文是否落 S3（content_ref = raw/{doc_id}.pdf）
    from app.services.storage import get_storage, reset_storage  # noqa: E402

    reset_storage()
    st = get_storage()
    key = f"raw/{doc_id}.pdf"
    metrics["storage"] = {
        "backend": type(st).__name__,
        "content_ref": key,
        "in_s3": st.exists(key),
        "size_bytes": (st.size(key) if st.exists(key) else None),
    }

    # 4) 检索（计时 + 结果 + 准确率）
    t0 = time.perf_counter()
    rr = requests.post(
        f"{BASE}/v1/retrieve",
        headers=auth(tok),
        json={"query": RETRIEVE_QUERY, "doc_types": ["意见"], "top_n": 4, "rerank": True},
        timeout=60,
    )
    ret_ms = (time.perf_counter() - t0) * 1000
    rr.raise_for_status()
    rd = rr.json()
    cits = rd["citations"]

    def _relevant(c: dict) -> bool:
        return GT_MARKER in (c.get("snippet") or "") or GT_MARKER in (c.get("content") or "")

    rel_flags = [_relevant(c) for c in cits]
    top1 = rel_flags[0] if rel_flags else False
    metrics["retrieve"] = {
        "query": RETRIEVE_QUERY,
        "latency_ms": round(ret_ms, 1),
        "total_matched": rd["total_matched"],
        "top_vector_distance": rd.get("top_vector_distance"),
        "keyword_hits": rd.get("keyword_hits"),
        "truncated_by_security": rd.get("truncated_by_security"),
        "n_citations": len(cits),
        "scores": [round(c["score"], 3) for c in cits],
        "relevant_flags": rel_flags,
        "top1_relevant": top1,
        "relevant_retrieved": any(rel_flags),
        "precision_at_k": round(sum(rel_flags) / len(rel_flags), 3) if rel_flags else 0.0,
        "gt_marker": GT_MARKER,
        "citations": [
            {"score": round(c["score"], 3), "snippet": (c.get("snippet") or "")[:80]} for c in cits
        ],
    }

    # 5) 生成（计时 + 结果）
    t0 = time.perf_counter()
    gr = requests.post(
        f"{BASE}/v1/generate",
        headers=auth(tok),
        json={
            "query": GEN_QUERY,
            "doc_types": ["意见"],
            "template_doc_type": "意见",
            "max_clearance": "INTERNAL",
            "rerank": False,
        },
        timeout=240,
    )
    gen_ms = (time.perf_counter() - t0) * 1000
    gr.raise_for_status()
    gd = gr.json()
    ans = gd.get("answer", "")
    metrics["generate"] = {
        "query": GEN_QUERY,
        "latency_ms": round(gen_ms, 1),
        "model": gd.get("model"),
        "answer_chars": len(ans),
        "n_citations": len(gd.get("citations", [])),
        "sensitive_hits": gd.get("sensitive_hits", []),
        "truncated_by_security": gd.get("truncated_by_security"),
        # 简易忠实度启发式：答案是否引用源关键术语
        "faithful_heuristic": all(k in ans for k in ["医保"]) and ("参保" in ans or "缴费" in ans),
        "answer_head": ans[:160],
        "answer": ans,
    }

    OUT.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k != "generate" or True}, ensure_ascii=False, indent=2))
    print(f"\n[metrics] 已写入 {OUT}")


def auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


if __name__ == "__main__":
    main()
