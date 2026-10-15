"""一次性：把切分完整性「一期」的新参数写入运行库 ConfigKV（经配置中心 API）。

背景：运行系统读 DB ConfigKV（改库即生效），仅改 runtime_config.py 代码默认值
只影响全新库；故需经 PUT /v1/config/items 更新当前库，使新参数立即生效。

用法：python tools/update_retrieval_config.py
"""

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8000/v1")

# 切分完整性一期目标值（与 runtime_config.py 新默认值一致）
TARGETS = {
    "small_max_tokens": 300,
    "small_overlap_tokens": 40,
    "large_max_tokens": 2500,
    "large_overlap_tokens": 200,
    "parent_inject_max_count": 3,
    "parent_inject_max_tokens": 6000,
}


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> None:
    st, raw = call("POST", "/auth/login", body={"username": "admin", "password": "admin123"})
    tok = json.loads(raw).get("access_token", "") if st == 200 else ""
    if not tok:
        print("[FAIL] 登录失败", st, raw[:200])
        return
    print("[PASS] 登录")

    ok = 0
    for key, val in TARGETS.items():
        st, raw = call(
            "PUT",
            "/config/items",
            token=tok,
            body={
                "category": "retrieval",
                "key": key,
                "value": {"value": val},
                "is_active": True,
            },
        )
        good = st == 200
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] PUT {key}={val} (HTTP {st})")
        if not good:
            print("   resp:", raw[:200])

    # 回读校验：确认库里已是目标值
    st, raw = call("GET", "/config/items?category=retrieval", token=tok)
    if st == 200:
        items = {it["key"]: (it.get("value") or {}).get("value") for it in json.loads(raw)}
        print("---- 回读校验 ----")
        allok = True
        for key, val in TARGETS.items():
            got = items.get(key)
            same = got == val
            allok &= same
            print(f"  {key}: 库中={got} 目标={val} {'OK' if same else 'MISMATCH'}")
        print(f"[{'PASS' if allok else 'FAIL'}] 回读全部一致" if allok else "[FAIL] 存在不一致")
    print(f"\n结果：PUT {ok}/{len(TARGETS)} 成功")


if __name__ == "__main__":
    main()
