"""Task #103 验证：捕获起草 SSE 流的事件类型（重点看 plan/step_start 是否出现）。"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def login():
    req = urllib.request.Request(
        f"{BASE}/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["access_token"]


def capture_first(token):
    payload = {
        "query": "关于开展第三季度安全生产大检查的通知",
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
    types = []
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(16384)  # 首段：应含 plan + step_start(retrieve) 等
            for line in raw.decode("utf-8", "ignore").split("\n"):
                if line.startswith("data:"):
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    types.append(ev.get("type"))
                    if ev.get("type") == "plan":
                        print("  plan.steps =", [s["action"] for s in ev.get("steps", [])])
                    if ev.get("type") == "step_start":
                        print(f"  step_start: {ev.get('id')}/{ev.get('action')}")
                    if ev.get("type") == "step_end":
                        print(f"  step_end: {ev.get('id')} ok={ev.get('ok')} summary={ev.get('summary')!r}")
            r.close()
    except Exception as e:
        print("  读取中断:", type(e).__name__, str(e)[:80])
    return types


def main():
    token = login()
    types = capture_first(token)
    print("事件序列(首段):", types)
    has_plan = "plan" in types
    has_step_start = "step_start" in types
    print(f"plan 事件: {'✓' if has_plan else '✗'}  step_start 事件: {'✓' if has_step_start else '✗'}")
    if has_plan and has_step_start:
        print("[OK] Task #103 后端步骤事件已正常流出")
    else:
        print("[FAIL] 未捕获到 plan/step_start，检查后端是否重载了新代码")


if __name__ == "__main__":
    main()
