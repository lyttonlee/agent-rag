"""验证导出接口（基于已生成内容，不重新生成）。

流程：登录 → 建 draft 会话 → 追加 assistant 消息（模拟已生成草稿）
→ 按 message_id 导出 docx（预期 200 + docx blob）
→ 边界：缺 message_id 取最新 / 错误 message_id 400 / 无 assistant 400
"""
import json
import os
import urllib.request
import urllib.error

BASE = os.environ.get("BASE", "http://127.0.0.1:8000/v1")
PASS = 0
FAIL = 0


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read()
            return r.status, ctype, raw
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main():
    print("  [INFO] BASE =", BASE)
    # 1) 登录
    st, _, raw = call("POST", "/auth/login", body={"username": "admin", "password": "admin123"})
    tok = json.loads(raw).get("access_token", "") if st == 200 else ""
    check("登录", st == 200 and tok, f"(HTTP {st})")
    if not tok:
        print("登录失败，终止"); return

    # 2) 建 draft 会话
    st, _, raw = call("POST", "/conversations", token=tok,
                      body={"conv_type": "draft", "title": "验证导出_临时"})
    print("  [DEBUG] create raw:", raw[:300])
    conv = json.loads(raw) if st in (200, 201) else {}
    cid = conv.get("id", "")
    check("建会话", st == 201 and cid, f"(HTTP {st})")

    # 3) 追加一条 assistant 消息（模拟已生成草稿）
    st, _, raw = call("POST", f"/conversations/{cid}/messages", token=tok,
                      body={"role": "assistant",
                            "content": "关于开展第三季度安全生产大检查的通知\n各单位：\n一、检查时间。\n二、检查内容。\n特此通知。",
                            "citations": [{"document_id": "doc-not-exist", "title": "某安全文件", "doc_no": "安〔2026〕1号"}],
                            "sensitive_hits": []})
    mid = json.loads(raw).get("id", "") if st == 201 else ""
    check("追加 assistant 消息", st == 201 and mid, f"(HTTP {st})")

    # 4) 按 message_id 导出 docx（预期 200 + docx blob）
    st, ctype, raw = call("POST", "/export/docx", token=tok,
                          body={"conversation_id": cid, "message_id": mid})
    is_docx = "wordprocessingml" in ctype or raw[:2] == b"PK"
    if not (st == 200 and is_docx):
        print("  [DEBUG] export raw:", raw[:400])
    check("按 message_id 导出 docx", st == 200 and is_docx, f"(HTTP {st}, ctype={ctype}, bytes={len(raw)})")

    # 5) 缺 message_id → 取最新 assistant
    st, ctype, raw = call("POST", "/export/docx", token=tok, body={"conversation_id": cid})
    is_docx2 = "wordprocessingml" in ctype or raw[:2] == b"PK"
    check("缺 message_id 取最新版", st == 200 and is_docx2, f"(HTTP {st})")

    # 6) 错误 message_id → 400
    st, _, _ = call("POST", "/export/docx", token=tok,
                    body={"conversation_id": cid, "message_id": "nonexistent-id"})
    check("错误 message_id → 400", st == 400, f"(HTTP {st})")

    # 7) 无 assistant 的会话 → 400
    st, _, raw = call("POST", "/conversations", token=tok, body={"conv_type": "draft", "title": "空会话_临时"})
    cid2 = json.loads(raw).get("id", "")
    st2, _, _ = call("POST", "/export/docx", token=tok, body={"conversation_id": cid2})
    check("无 assistant → 400", st2 == 400, f"(HTTP {st2})")

    # 8) 会话不存在 → 404
    st, _, _ = call("POST", "/export/docx", token=tok, body={"conversation_id": "no-such-conv"})
    check("会话不存在 → 404", st == 404, f"(HTTP {st})")

    # 清理
    call("DELETE", f"/conversations/{cid}", token=tok)
    call("DELETE", f"/conversations/{cid2}", token=tok)
    print(f"\n===== 结果：PASS={PASS} FAIL={FAIL} =====")


if __name__ == "__main__":
    main()
