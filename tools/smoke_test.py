"""功能冒烟测试：清库后走一轮核心链路，验证系统可用性。

链路（按用户操作顺序）：
  健康 → 登录 → 空间 CRUD → OFD 上传入库（轮询到 INDEXED）→ PDF 上传入库
  → DOCX 上传入库 → 六级密级归一（中文名/历史代码）→ 文档列表/详情/统计
  → 检索 → 生成 → 起草会话+导出（公文版式校验）→ 智能问答(chat) → 会话历史
  搜索（关键字/明确ID）→ 模板库 → 配置中心 → 审计日志

与 tools/verify_ofd_ingest.py 的区别：那个是 OFD 深度验证（15 篇全量 + 红线指标），
本脚本是**广度冒烟**——每个核心接口至少走通一次，关注"能不能用"而非"多好"。

前置：已执行 tools/reset_all_data.py 且后端已重启（种子数据已重建）。

用法：
    python tools/smoke_test.py
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE", "http://127.0.0.1:8000/v1")
USER = os.environ.get("SMOKE_USER", "admin")
PWD = os.environ.get("SMOKE_PWD", "admin123")
OFD_DIR = ROOT / "ndrc_ofd_files"
PDF_DIR = ROOT / "sichuan_gov_docs"

PASS = 0
FAIL = 0
ROWS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        ROWS.append(("PASS", name, extra))
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        ROWS.append(("FAIL", name, extra))
        print(f"  [FAIL] {name} {extra}")
    return bool(cond)


def make_docx() -> bytes:
    """构造一个最小 DOCX（冒烟用，避免依赖外部样本）。"""
    from docx import Document  # noqa: PLC0415

    d = Document()
    d.add_heading("关于开展安全生产大检查的通知", 0)
    d.add_paragraph("各省、自治区、直辖市人民政府，国务院各部委、各直属机构：")
    d.add_paragraph(
        "为深入贯彻落实党中央、国务院关于安全生产的决策部署，全面排查整治风险隐患，"
        "经国务院同意，现就开展安全生产大检查有关事项通知如下："
    )
    d.add_paragraph("一、总体要求。坚持人民至上、生命至上，统筹发展和安全。")
    d.add_paragraph("二、检查范围。覆盖矿山、危化品、交通、建筑、消防等重点行业领域。")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def wait_job(tok: str, jid: str, timeout: int = 300) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(
            f"{BASE}/documents/jobs/{jid}", headers={"Authorization": f"Bearer {tok}"}, timeout=20
        )
        j = r.json()
        if j.get("status") in ("INDEXED", "FAILED"):
            return j
        time.sleep(2)
    return {"status": "TIMEOUT"}


def main() -> int:
    print("=" * 74)
    print("功能冒烟测试（清库后）")
    print("=" * 74)

    # ---- 1. 健康检查 ----
    r = requests.get("http://127.0.0.1:8000/health", timeout=15)
    h = r.json() if r.status_code == 200 else {}
    check("健康检查 /health", r.status_code == 200 and h.get("status") == "ok",
          f"(db={h.get('db', {}).get('ok')}, storage={h.get('storage', {}).get('ok')})")

    # ---- 2. 登录 ----
    r = requests.post(f"{BASE}/auth/login", json={"username": USER, "password": PWD}, timeout=20)
    tok = r.json().get("access_token", "") if r.status_code == 200 else ""
    check("登录", bool(tok), f"(HTTP {r.status_code})")
    if not tok:
        print("登录失败，冒烟终止")
        return 1
    H = {"Authorization": f"Bearer {tok}"}

    # ---- 3. 空间 CRUD ----
    r = requests.post(f"{BASE}/spaces", json={"name": "冒烟测试空间", "code": f"smoke{int(time.time())%100000}"}, headers=H, timeout=20)
    sp = r.json() if r.status_code in (200, 201) else {}
    sid = sp.get("id", "")
    check("创建空间", bool(sid), f"(HTTP {r.status_code})")
    r = requests.get(f"{BASE}/spaces", headers=H, timeout=20)
    check("空间列表", r.status_code == 200 and any(s.get("id") == sid for s in r.json()), f"(共 {len(r.json())} 个)")
    r = requests.put(f"{BASE}/spaces/{sid}", json={"name": "冒烟空间-改名"}, headers=H, timeout=20)
    check("更新空间", r.status_code == 200, f"(HTTP {r.status_code})")

    # ---- 4. OFD 上传入库（本轮核心）----
    ofd_files = sorted(OFD_DIR.glob("*.ofd"))
    ofd = next((f for f in ofd_files if f.name.startswith("004")), ofd_files[0] if ofd_files else None)
    if ofd is None:
        check("OFD 上传入库", False, "(未找到 OFD 语料)")
    else:
        with open(ofd, "rb") as f:
            r = requests.post(
                f"{BASE}/documents/upload",
                files={"file": (ofd.name, f, "application/octet-stream")},
                data={"title": "冒烟_OFD", "doc_type": "通知", "security_level": "INTERNAL"},
                headers=H, timeout=120,
            )
        job = r.json() if r.status_code == 200 else {}
        jid = job.get("id", "")
        done = wait_job(tok, jid) if jid else {}
        check("OFD 上传入库", done.get("status") == "INDEXED",
              f"({ofd.name[:26]}… → {done.get('status')}, 切片 {done.get('total_chunks')}, "
              f"{done.get('error_message') or ''})")
        ofd_doc_id = done.get("document_id", "")

    # ---- 5. PDF 上传入库 ----
    pdf = PDF_DIR / "003_川办发36号公开版.pdf"
    if pdf.exists():
        with open(pdf, "rb") as f:
            r = requests.post(
                f"{BASE}/documents/upload",
                files={"file": (pdf.name, f, "application/pdf")},
                data={"title": "冒烟_PDF", "doc_type": "意见", "security_level": "INTERNAL"},
                headers=H, timeout=120,
            )
        job = r.json() if r.status_code == 200 else {}
        done = wait_job(tok, job.get("id", "")) if job.get("id") else {}
        check("PDF 上传入库", done.get("status") == "INDEXED",
              f"({pdf.name[:20]}… → {done.get('status')}, 切片 {done.get('total_chunks')})")

    # ---- 6. DOCX 上传入库 ----
    r = requests.post(
        f"{BASE}/documents/upload",
        files={
            "file": (
                "冒烟安全生产通知.docx",
                io.BytesIO(make_docx()),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"title": "冒烟_DOCX", "doc_type": "通知", "security_level": "INTERNAL"},
        headers=H, timeout=120,
    )
    job = r.json() if r.status_code == 200 else {}
    done = wait_job(tok, job.get("id", "")) if job.get("id") else {}
    check("DOCX 上传入库", done.get("status") == "INDEXED",
          f"(→ {done.get('status')}, 切片 {done.get('total_chunks')})")

    # ---- 7. 六级密级归一（中文名 / 历史代码 → 规范枚举）----
    # 7a. 上传时传中文密级名 → 落库应为规范枚举 CORE_COMMERCIAL
    #     （upload 入口 normalize_security_level 归一，防御历史调用方零改造）
    r = requests.post(
        f"{BASE}/documents/upload",
        files={
            "file": (
                "冒烟商密归一.docx",
                io.BytesIO(make_docx()),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"title": "冒烟_商密归一", "doc_type": "通知", "security_level": "核心商密"},
        headers=H, timeout=120,
    )
    job = r.json() if r.status_code == 200 else {}
    done = wait_job(tok, job.get("id", "")) if job.get("id") else {}
    cc_doc_id = done.get("document_id", "")
    cc_level = "N/A"
    if cc_doc_id:
        r = requests.get(f"{BASE}/documents/{cc_doc_id}", headers=H, timeout=30)
        cc_level = (r.json() if r.status_code == 200 else {}).get("security_level", "N/A")
    check("密级归一：中文名「核心商密」→ CORE_COMMERCIAL",
          done.get("status") == "INDEXED" and cc_level == "CORE_COMMERCIAL",
          f"(job={done.get('status')}, 落库 {cc_level})")

    # 7b. 注册客户端传历史代码 RESTRICTED → 回显应归一为 COMMERCIAL
    #     （schema 层 AfterValidator 归一，存量接入零改造）
    r = requests.post(
        f"{BASE}/auth/clients/register",
        json={"name": "冒烟归一客户端", "scopes": ["retrieve"], "max_clearance": "RESTRICTED"},
        timeout=20,
    )
    cli = r.json() if r.status_code in (200, 201) else {}
    check("密级归一：历史代码 RESTRICTED → COMMERCIAL",
          cli.get("max_clearance") == "COMMERCIAL",
          f"(HTTP {r.status_code}, 回显 {cli.get('max_clearance')})")

    # ---- 8. 文档列表 / 详情 / 统计 ----
    r = requests.get(f"{BASE}/documents", headers=H, timeout=30)
    docs = r.json() if r.status_code == 200 else []
    check("文档列表", r.status_code == 200 and len(docs) >= 3, f"(共 {len(docs)} 篇)")
    if docs:
        did = docs[0]["id"]
        r = requests.get(f"{BASE}/documents/{did}", headers=H, timeout=30)
        det = r.json() if r.status_code == 200 else {}
        check("文档详情（含切片）", r.status_code == 200 and len(det.get("chunks", [])) > 0,
              f"(切片 {len(det.get('chunks', []))} 片)")
    r = requests.get(f"{BASE}/documents/stats", headers=H, timeout=30)
    st = r.json() if r.status_code == 200 else {}
    check("文档统计", r.status_code == 200 and st.get("total_documents", 0) >= 3,
          f"(文档 {st.get('total_documents')}, 切片 {st.get('total_chunks')})")

    # ---- 9. 检索 ----
    r = requests.post(f"{BASE}/retrieve", json={"query": "安全生产大检查", "top_n": 5}, headers=H, timeout=180)
    b = r.json() if r.status_code == 200 else {}
    cits = b.get("citations") or []
    check("检索 /retrieve", r.status_code == 200 and len(cits) > 0,
          f"(命中 {len(cits)} 片, keyword_hits={b.get('keyword_hits')})")

    # ---- 10. 生成 ----
    r = requests.post(f"{BASE}/generate", json={"query": "安全生产大检查的重点行业有哪些", "top_n": 3}, headers=H, timeout=800)
    g = r.json() if r.status_code == 200 else {}
    ans = g.get("answer", "")
    # 阈值 20 字：问题本身可能一句话可答（如"重点行业有哪些"→"矿山、危化品、
    # 交通、建筑、消防"），用字数衡量"可用"而非"详尽"。
    check("生成 /generate", r.status_code == 200 and len(ans) > 20,
          f"({len(ans)} 字, 引用 {len(g.get('citations') or [])} 条, 敏感词 {len(g.get('sensitive_hits') or [])})")

    # ---- 11. 起草会话 + 导出（公文版式校验）----
    # 导出走 render_docx：md 结构必须转成真实 Word 样式（字体/缩进/表格），
    # 不得残留 ## / ** 等 md 语法（用户 2026-10-15 需求）
    r = requests.post(f"{BASE}/conversations", json={"conv_type": "draft", "title": "冒烟起草"}, headers=H, timeout=20)
    dconv = r.json() if r.status_code in (200, 201) else {}
    dcid = dconv.get("id", "")
    check("创建起草会话", bool(dcid), f"(HTTP {r.status_code})")
    if dcid:
        # 手动追加一条含 md 结构的 assistant 消息（模拟模型产出，避免依赖真实生成）
        draft_md = (
            "# 关于开展安全生产大检查的通知\n\n"
            "各省、自治区、直辖市人民政府，国务院各部委、各直属机构：\n\n"
            "为深入贯彻落实党中央、国务院关于安全生产的决策部署，现就有关事项**通知如下**：\n\n"
            "## 一、总体要求\n\n"
            "坚持人民至上、生命至上，统筹发展和安全。\n\n"
            "## 二、检查范围\n\n"
            "| 行业领域 | 检查重点 |\n| --- | --- |\n| 矿山 | 矿井通风与瓦斯治理 |\n| 危化品 | 储运环节风险防控 |\n\n"
            "请各地区结合实际认真贯彻执行。\n\n"
            "国务院办公厅\n2026年10月15日"
        )
        r = requests.post(
            f"{BASE}/conversations/{dcid}/messages",
            json={"role": "assistant", "content": draft_md},
            headers=H, timeout=20,
        )
        check("追加起草消息", r.status_code in (200, 201), f"(HTTP {r.status_code})")
        # 导出 docx（不指定模板 → GB/T 9704 标准版式）并解包校验
        r = requests.post(
            f"{BASE}/export/docx",
            json={"conversation_id": dcid},
            headers=H, timeout=60,
        )
        blob = r.content if r.status_code == 200 else b""
        check("导出 docx", r.status_code == 200 and blob[:2] == b"PK",
              f"(HTTP {r.status_code}, {len(blob)} 字节)")
        if blob[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            check("导出版式：无 md 语法残留（##/**）",
                  ("##" not in xml) and ("**" not in xml))
            check("导出版式：正文仿宋_GB2312 落样", 'w:eastAsia="仿宋_GB2312"' in xml)
            check("导出版式：一级标题黑体落样", 'w:eastAsia="黑体"' in xml)
            check("导出版式：表格真实生成", "<w:tbl>" in xml)

    # ---- 12. 智能问答（chat 会话）----
    r = requests.post(f"{BASE}/conversations", json={"conv_type": "chat", "title": "冒烟问答"}, headers=H, timeout=20)
    conv = r.json() if r.status_code in (200, 201) else {}
    cid = conv.get("id", "")
    check("创建会话", bool(cid), f"(HTTP {r.status_code})")
    if cid:
        r = requests.post(
            f"{BASE}/conversations/{cid}/messages",
            json={"role": "user", "content": "安全生产大检查什么时候开展？"},
            headers=H, timeout=20,
        )
        check("追加消息", r.status_code in (200, 201), f"(HTTP {r.status_code})")

    # ---- 13. 会话历史搜索（关键字 / 明确 ID）----
    if cid:
        # 13a. 关键字命中消息内容（此前追加的"安全生产大检查什么时候开展？"）
        r = requests.get(
            f"{BASE}/conversations", params={"keyword": "安全生产大检查"}, headers=H, timeout=30
        )
        hits = r.json() if r.status_code == 200 else []
        hits = hits if isinstance(hits, list) else []
        kw_hit = any(c.get("id") == cid for c in hits)
        snippet = next((c.get("match_snippet") for c in hits if c.get("id") == cid), None)
        check("历史搜索：关键字命中", r.status_code == 200 and kw_hit,
              f"(命中 {len(hits)} 条)")
        check("历史搜索：match_snippet 片段",
              bool(snippet) and "安全生产" in (snippet or ""),
              f"({(snippet or '')[:40]}…)")
        # 13b. 明确 ID 精确查找（直达某条历史）
        r = requests.get(f"{BASE}/conversations", params={"id": cid}, headers=H, timeout=30)
        one = r.json() if r.status_code == 200 else []
        check("历史搜索：明确 ID 精确命中",
              r.status_code == 200 and isinstance(one, list) and len(one) == 1 and one[0].get("id") == cid,
              f"(HTTP {r.status_code})")
        # 13c. ID 不存在 → 404（与"关键字无匹配返空列表"语义区分）
        r = requests.get(
            f"{BASE}/conversations", params={"id": "conv-does-not-exist"}, headers=H, timeout=30
        )
        check("历史搜索：ID 不存在返回 404", r.status_code == 404,
              f"(HTTP {r.status_code})")

    # ---- 14. 模板库 ----
    r = requests.get(f"{BASE}/templates", headers=H, timeout=30)
    tpls = r.json() if r.status_code == 200 else []
    check("模板库列表", r.status_code == 200 and isinstance(tpls, list), f"(共 {len(tpls) if isinstance(tpls, list) else 0} 个)")

    # ---- 15. 配置中心 ----
    r = requests.get(f"{BASE}/config/items?category=retrieval", headers=H, timeout=30)
    cfgs = r.json() if r.status_code == 200 else []
    check("配置中心读取", r.status_code == 200 and len(cfgs) > 0,
          f"(检索配置 {len(cfgs)} 项)")

    # ---- 16. 审计日志 ----
    r = requests.get(f"{BASE}/audit/logs?page=1&page_size=5", headers=H, timeout=30)
    check("审计日志", r.status_code == 200, f"(HTTP {r.status_code})")

    # ---- 17. 删除冒烟空间（收尾）----
    r = requests.delete(f"{BASE}/spaces/{sid}", headers=H, timeout=20)
    check("删除空间（收尾）", r.status_code in (200, 204), f"(HTTP {r.status_code})")

    # ---- 汇总 ----
    print("\n" + "=" * 74)
    print("冒烟结果汇总")
    print("=" * 74)
    for status, name, extra in ROWS:
        mark = "✅" if status == "PASS" else "❌"
        print(f"  {mark} {name:24s} {extra[:60]}")
    print(f"\n合计：{PASS} PASS / {FAIL} FAIL")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
