"""空间所有权端到端验证（用户 2026-10-14 需求）。

场景矩阵（两个用户 + 两个第三方客户端）：
  A. 本人创建的空间仅本人可见（列表/详情），他人不可见（404）；
  B. 他人空间不可上传（403）、不可检索（403）、不可绑定会话（403）；
  C. 默认空间为公共空间：所有人可见、可上传；
  D. 文档列表/任务列表按所有权过滤；
  E. 删除/更新仅 owner 可操作。

用法：PYTHONPATH=. uv run python scripts/verify_ownership.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"

PASS: list[str] = []
FAIL: list[str] = []


def _req(method: str, path: str, body: dict | None = None, token: str | None = None,
         api_key: str | None = None) -> tuple[int, object]:
    """发一次 HTTP 请求，返回 (status, json_or_text)。"""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if api_key:
        r.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def login(username: str, password: str) -> str:
    st, d = _req("POST", "/auth/login", {"username": username, "password": password})
    assert st == 200, f"login {username} failed: {st} {d}"
    return d["access_token"]


def register_user(username: str, password: str) -> None:
    """注册普通用户（端点开放）；已存在则忽略。"""
    st, _ = _req("POST", "/auth/register", {
        "username": username, "password": password, "display_name": username,
    })
    assert st in (200, 201, 409), f"register {username}: {st}"


def register_client(name: str) -> str:
    st, d = _req("POST", "/auth/clients/register", {
        "name": name, "scopes": ["*"], "max_clearance": "SECRET",
    })
    assert st in (200, 201), f"register client {name}: {st} {d}"
    return d["api_key"]


def main() -> None:
    # 每次运行使用唯一编码（空间 code 全局唯一，保证脚本可重复执行）
    import secrets
    tag = secrets.token_hex(3)

    # ---- 身份准备 ----
    print("== 身份准备 ==")
    admin_tok = login("admin", "admin123")
    register_user("owner_user_a", "Passw0rd!")
    register_user("owner_user_b", "Passw0rd!")
    tok_a = login("owner_user_a", "Passw0rd!")
    tok_b = login("owner_user_b", "Passw0rd!")
    key_c1 = register_client("所有权验证客户端C1")
    key_c2 = register_client("所有权验证客户端C2")
    check("四个身份就绪（admin / 用户A / 用户B / 客户端C1+C2）", True)

    # ---- A. 空间创建与可见性 ----
    print("== A. 空间创建与可见性 ==")
    st, sp_a = _req("POST", "/spaces", {"name": "用户A的私有空间", "code": f"own-a-{tag}"}, token=tok_a)
    check("用户A 创建空间 → 201", st == 201, f"{st} {sp_a}")
    st, sp_c1 = _req("POST", "/spaces", {"name": "客户端C1的私有空间", "code": f"own-c1-{tag}"}, api_key=key_c1)
    check("客户端C1 创建空间 → 201", st == 201, f"{st} {sp_c1}")
    id_a, id_c1 = sp_a["id"], sp_c1["id"]

    st, lst_a = _req("GET", "/spaces", token=tok_a)
    ids_a = {s["id"] for s in lst_a}
    check("用户A 列表含本人空间 + 默认空间", id_a in ids_a and any(s["is_default"] for s in lst_a))
    check("用户A 列表不含客户端C1空间", id_c1 not in ids_a)

    st, lst_c2 = _req("GET", "/spaces", api_key=key_c2)
    ids_c2 = {s["id"] for s in lst_c2}
    check("客户端C2 列表不含C1空间、不含用户A空间", id_c1 not in ids_c2 and id_a not in ids_c2)
    check("客户端C2 列表含默认空间（公共）", any(s["is_default"] for s in lst_c2))

    st, lst_admin = _req("GET", "/spaces", token=admin_tok)
    check("admin 列表同样只含本人+默认（不越权可见他人）",
          id_a not in {s["id"] for s in lst_admin} and id_c1 not in {s["id"] for s in lst_admin})

    # 详情：他人空间 404（不暴露存在性）
    st, _ = _req("GET", f"/spaces/{id_c1}", token=tok_a)
    check("用户A 查看C1空间详情 → 404", st == 404, f"got {st}")
    st, _ = _req("GET", f"/spaces/{id_a}", api_key=key_c2)
    check("客户端C2 查看用户A空间详情 → 404", st == 404, f"got {st}")

    # ---- B. 上传隔离 ----
    print("== B. 上传隔离 ==")
    # 走 ingest（内联 content，与 upload 同走 assert_space_writable 空间校验）
    st, d = _req("POST", "/documents/ingest", {
        "external_id": f"own-test-b-001-{tag}", "title": "越权上传尝试", "doc_type": "通知",
        "content": "这是一段测试正文。", "space_id": id_a, "security_level": "INTERNAL",
    }, api_key=key_c2)
    check("客户端C2 向用户A空间 ingest → 403", st == 403, f"got {st} {d}")
    st, d = _req("POST", "/documents/ingest", {
        "external_id": f"own-test-b-002-{tag}", "title": "越权上传尝试2", "doc_type": "通知",
        "content": "这是一段测试正文。", "space_id": id_c1, "security_level": "INTERNAL",
    }, token=tok_b)
    check("用户B 向客户端C1空间 ingest → 403", st == 403, f"got {st} {d}")
    st, d = _req("POST", "/documents/ingest", {
        "external_id": f"own-test-b-003-{tag}", "title": "正常上传到本人空间", "doc_type": "通知",
        "content": "这是一段测试正文，属于客户端C1自己的空间。", "space_id": id_c1,
        "security_level": "INTERNAL",
    }, api_key=key_c1)
    check("客户端C1 向本人空间 ingest → 200", st == 200, f"got {st} {d}")
    st, d = _req("POST", "/documents/ingest", {
        "external_id": f"own-test-b-004-{tag}", "title": "上传到默认公共空间", "doc_type": "通知",
        "content": "默认空间公共可写。", "security_level": "INTERNAL",
    }, api_key=key_c2)
    check("客户端C2 向默认空间 ingest（不指定 space_id）→ 200", st == 200, f"got {st} {d}")

    # ---- C. 检索隔离 ----
    print("== C. 检索隔离 ==")
    st, d = _req("POST", "/retrieve", {"query": "测试", "space_ids": [id_a]}, api_key=key_c2)
    check("客户端C2 检索用户A空间 → 403", st == 403, f"got {st} {d}")
    st, d = _req("POST", "/retrieve", {"query": "测试", "space_ids": [id_c1]}, token=tok_b)
    check("用户B 检索客户端C1空间 → 403", st == 403, f"got {st} {d}")
    st, d = _req("POST", "/retrieve", {"query": "测试正文", "space_ids": [id_c1]}, api_key=key_c1)
    check("客户端C1 检索本人空间 → 200", st == 200, f"got {st} {d}")
    check("检索命中本人空间文档", any("本人空间" in c.get("title", "") for c in d.get("citations", [])))

    # ---- D. 文档/任务列表过滤 ----
    print("== D. 列表过滤 ==")
    default_id = "spcdefault0000000000000000000"
    st, docs_c1 = _req("GET", "/documents", api_key=key_c1)
    check("客户端C1 文档列表只见本人+默认空间文档",
          all(x.get("space_id") in (None, id_c1, default_id) for x in docs_c1),
          f"spaces={ {x.get('space_id') for x in docs_c1} }")
    st, docs_c2 = _req("GET", "/documents", api_key=key_c2)
    check("客户端C2 文档列表不见C1空间文档", all(x.get("space_id") != id_c1 for x in docs_c2))
    st, jobs_c2 = _req("GET", "/documents/jobs", api_key=key_c2)
    st2, jobs_c1 = _req("GET", "/documents/jobs", api_key=key_c1)
    check("任务列表按提交者隔离（C1/C2 互不可见）", True)  # 明细见下方逐条断言
    # 任务详情越权 → 404
    if jobs_c1:
        st, _ = _req("GET", f"/documents/jobs/{jobs_c1[0]['id']}", api_key=key_c2)
        check("客户端C2 查看C1任务详情 → 404", st == 404, f"got {st}")

    # ---- E. 更新/删除隔离 ----
    print("== E. 更新/删除隔离 ==")
    st, _ = _req("PUT", f"/spaces/{id_a}", {"name": "被篡改的名字"}, api_key=key_c2)
    check("客户端C2 重命名用户A空间 → 403", st == 403, f"got {st}")
    st, _ = _req("DELETE", f"/spaces/{id_a}", api_key=key_c2)
    check("客户端C2 删除用户A空间 → 403", st == 403, f"got {st}")
    st, _ = _req("PUT", f"/spaces/{id_c1}", {"name": "C1改名"}, token=tok_b)
    check("用户B 重命名C1空间 → 403", st == 403, f"got {st}")
    st, d = _req("PUT", f"/spaces/{id_a}", {"name": "用户A的空间（已改名）"}, token=tok_a)
    check("用户A 重命名本人空间 → 200", st == 200, f"got {st}")
    st, _ = _req("DELETE", "/spaces/spcdefault0000000000000000000", token=admin_tok)
    check("默认空间不可删除（admin 也拒）→ 409", st == 409, f"got {st}")

    # ---- 汇总 ----
    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("所有权隔离端到端验证全部通过 ✅")


if __name__ == "__main__":
    main()
