"""验证起草历史会话落库（Task #102 修复验证，快速版）。

要点：起草会话在 stream_generate 的 meta 事件之前（检索/生成之前）已同步
落库。因此只要 SSE 响应一开始流出（首个数据块到达），会话即已创建。
本脚本读取首个数据块后即中断连接，再去 draft 列表核对，避免等待整段生成。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
LOG = "tools/verify_draft_conv.log"


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def login():
    req = urllib.request.Request(
        f"{BASE}/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["access_token"]


def stream_first_chunk(token):
    payload = {
        "query": "关于开展第三季度安全生产大检查的通知（验证会话落库）",
        "template_doc_type": "通知",
        "caller_scope": [],
        "max_clearance": "INTERNAL",
        "rerank": False,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/generate/stream",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            # 读首块（含 meta 事件），足以触发服务端创建会话
            raw = r.read(4096)
            # 主动中断连接（服务端会收到 GeneratorExit，不影响已落库会话）
            r.close()
    except urllib.error.HTTPError as e:
        return raw, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return raw, f"read 中断: {type(e).__name__}"
    return raw, None


def parse_conversation_id(raw):
    text = raw.decode("utf-8", "ignore")
    for line in text.split("\n"):
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("conversation_id"):
                return ev["conversation_id"], ev.get("type")
    return None, None


def list_drafts(token):
    req = urllib.request.Request(
        f"{BASE}/v1/conversations?conv_type=draft",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def delete_conv(token, cid):
    req = urllib.request.Request(
        f"{BASE}/v1/conversations/{cid}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode()
    except Exception as e:
        return f"删除失败: {e}"


def main():
    open(LOG, "w", encoding="utf-8").close()
    token = login()
    log("[1] 登录成功")
    before = list_drafts(token)
    log(f"[2] 当前 draft 会话数: {len(before)}")

    raw, err = stream_first_chunk(token)
    if err:
        log(f"[3] 读流提示: {err}")
    cid, evtype = parse_conversation_id(raw)
    log(f"[3] 首个事件类型: {evtype}, 回传 conversation_id: {cid}")

    after = list_drafts(token)
    log(f"[4] 流后 draft 会话数: {len(after)}")

    if cid:
        hit = next((c for c in after if c["id"] == cid), None)
        if hit:
            log(f"[5] 命中新建会话: title={hit['title']!r} type={hit['conv_type']} "
                f"msg_count={hit['message_count']} doc_type={hit.get('doc_type')}")
            log("[6] 清理: " + delete_conv(token, cid))
            log("[OK] Task #102 通过：起草会话已自动创建并在 draft 列表可见")
        else:
            log("[FAIL] conversation_id 未出现在 draft 列表")
    else:
        # 未从首块拿到 id（极端情况）：检查是否新增了会话
        new = [c for c in after if c["id"] not in {x["id"] for x in before}]
        log(f"[WARN] 首块未含 conversation_id；新增会话: {[(c['id'], c['title']) for c in new]}")
        for c in new:
            log("   清理: " + delete_conv(token, c["id"]))


if __name__ == "__main__":
    main()
    sys.exit(0)
