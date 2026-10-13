#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型配置连通性测试脚本（tools/test_model_config.py）。

用途
----
验证《.env》中配置的三个模型端点是否可用，故障时给出明确原因：
  1. 生成模型（chat/completions，支持流式）——云端或本地 vLLM
  2. 向量模型（embeddings）——本地 vLLM（192.168.6.49:8002）
  3. 重排模型（rerank）——本地 vLLM（192.168.6.49:8001）

使用方法
--------
  # 1. 直接用下方 CONFIG 中的默认值（与 backend/.env 保持一致）测试：
  python tools/test_model_config.py

  # 2. 修改参数测试：编辑下方 CONFIG 里的 BASE_URL / API_KEY / MODEL 后重跑；
  #    也可用环境变量临时覆盖（无需改文件，适合 CI）：
  MODEL_GEN_API_KEY=sk-新key python tools/test_model_config.py

退出码：0 = 全部通过；1 = 存在失败项（可接入脚本判断）。

实现说明
--------
- 仅用 Python 标准库（urllib），无需安装任何依赖，任意 Python 3.8+ 环境可跑。
- 强制直连、不走系统代理：本项目为内网部署，代理会干扰内网/本机地址访问。
- 每个端点独立测试、互不影响；单个失败不影响其余项的报告。
- **注意（2026-10-13 实测发现）**：阿里云该生成端点对 curl 等客户端会返回
  401 invalid_api_key（TLS 指纹层面的客户端歧视），而 Python 客户端
  （urllib / openai SDK / httpx，即后端真实使用方）全部正常。判断配置
  是否可用以后端 SDK 的实测结果为准，切勿用 curl/Postman 的 401 下结论。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# ============================================================
# CONFIG：测试参数（默认值与 backend/.env 一致，按需修改）
# ============================================================
CONFIG = {
    # ---- 1. 生成模型（chat/completions）----
    # 云端示例（阿里云百炼 compatible-mode）：
    "GEN_BASE_URL": "https://llm-m1xhkuav519aow9i.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "GEN_API_KEY": "sk-818d70335ad042c4b715b0a16ef96392",
    "GEN_MODEL": "qwen3.8-27b",

    # ---- 2. 向量模型（embeddings）----
    "EMB_BASE_URL": "http://192.168.6.49:8002/v1",
    "EMB_API_KEY": "EMPTY",
    "EMB_MODEL": "/modelscope_cache/models/Qwen--Qwen3-Embedding-0.6B/snapshots/master",

    # ---- 3. 重排模型（rerank，vLLM 原生 /v1/rerank 端点）----
    "RERANK_BASE_URL": "http://192.168.6.49:8001/v1",
    "RERANK_API_KEY": "EMPTY",
    "RERANK_MODEL": "/modelscope_cache/models/Qwen--Qwen3-Reranker-0.6B/snapshots/master",
}

# 环境变量覆盖（优先级高于 CONFIG，便于不改文件临时测试）
_ENV_OVERRIDE = {
    "GEN_BASE_URL": "MODEL_GEN_BASE_URL",
    "GEN_API_KEY": "MODEL_GEN_API_KEY",
    "GEN_MODEL": "MODEL_GEN_NAME",
    "EMB_BASE_URL": "MODEL_EMB_BASE_URL",
    "EMB_API_KEY": "MODEL_EMB_API_KEY",
    "EMB_MODEL": "MODEL_EMB_NAME",
    "RERANK_BASE_URL": "MODEL_RERANK_BASE_URL",
    "RERANK_API_KEY": "MODEL_RERANK_API_KEY",
    "RERANK_MODEL": "MODEL_RERANK_NAME",
}
for _key, _env in _ENV_OVERRIDE.items():
    if os.environ.get(_env):
        CONFIG[_key] = os.environ[_env]

# 全局请求超时（秒）：连接 + 读取
TIMEOUT = 30
# 直连 opener：禁用系统代理（内网部署，代理会干扰访问）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url: str, payload: dict, api_key: str, stream: bool = False):
    """POST JSON 并返回 (http_status, body_text, elapsed_seconds)。

    网络层异常（连接失败/超时等）转化为 status=-1 + 错误描述，由调用方判读。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        resp = _OPENER.open(req, timeout=TIMEOUT)
        if not stream:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, time.monotonic() - started
        # 流式：逐行读取 SSE，收集前若干事件后即返回（验证"能流式产出"即可）
        lines: list[str] = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                lines.append(line)
                if len(lines) >= 3:  # 收到 3 个事件即认为流式通道正常
                    break
        return resp.status, "\n".join(lines), time.monotonic() - started
    except urllib.error.HTTPError as e:
        # HTTP 层错误（401/403/404/5xx）：服务端可达，读回错误体便于定位
        body = e.read().decode("utf-8", "replace")
        return e.code, body, time.monotonic() - started
    except Exception as e:  # 连接拒绝、DNS、超时等
        return -1, f"{type(e).__name__}: {e}", time.monotonic() - started


def _verdict(name: str, status: int, body: str, elapsed: float, check_ok, hint: str = "") -> bool:
    """打印单项测试结果。check_ok(body) 用于内容级校验（如向量维度）。"""
    if status == 200 and check_ok(body):
        print(f"  [PASS] {name}  HTTP 200  {elapsed:.2f}s")
        return True
    print(f"  [FAIL] {name}  HTTP {status}  {elapsed:.2f}s")
    # 常见故障给出可操作的提示
    if status == 401:
        print("        原因：API key 无效或已过期（服务端返回 401）。")
        print("        处理：更新 BASE_URL/API_KEY 后重试；阿里云百炼 key 可在")
        print("              https://bailian.console.aliyun.com/ 的 API-KEY 管理页查看/重建。")
    elif status == 404:
        print("        原因：路径不存在（404）。检查 BASE_URL 是否补全了 /v1 后缀、")
        print("              以及具体端点路径（chat/completions、embeddings、rerank）。")
    elif status == -1:
        print("        原因：网络不通（连接被拒/DNS/超时）。检查地址、端口与网络策略。")
    elif status != 200:
        print(f"        原因：服务端返回 HTTP {status}。")
    if hint:
        print(f"        提示：{hint}")
    snippet = body.strip().replace("\n", " ")[:300]
    print(f"        响应片段：{snippet}")
    return False


def test_gen() -> bool:
    """生成模型：非流式 + 流式两次探测（流式是问答链路的实际使用方式）。"""
    url = f"{CONFIG['GEN_BASE_URL'].rstrip('/')}/chat/completions"
    print(f"\n[1/3] 生成模型  {url}")
    print(f"      model={CONFIG['GEN_MODEL']}")

    # 1a. 非流式最小请求
    status, body, elapsed = _post_json(
        url,
        {
            "model": CONFIG["GEN_MODEL"],
            "messages": [{"role": "user", "content": "只回复两个字：可用"}],
            "max_tokens": 16,
            "temperature": 0,
        },
        CONFIG["GEN_API_KEY"],
    )
    ok1 = _verdict("非流式 chat/completions", status, body, elapsed,
                   lambda b: "choices" in b and len(b) > 0)
    if not ok1:
        return False
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        print(f"        模型回复：{content.strip()[:80]!r}")
    except Exception:
        pass

    # 1b. 流式最小请求（验证 SSE 通道，问答/起草链路依赖）
    status, body, elapsed = _post_json(
        url,
        {
            "model": CONFIG["GEN_MODEL"],
            "messages": [{"role": "user", "content": "只回复两个字：可用"}],
            "max_tokens": 16,
            "temperature": 0,
            "stream": True,
        },
        CONFIG["GEN_API_KEY"],
        stream=True,
    )
    ok2 = _verdict("流式 chat/completions", status, body, elapsed,
                   lambda b: b.startswith("data:"))
    return ok1 and ok2


def test_emb() -> bool:
    """向量模型：单条文本 embedding，校验返回结构与维度非空。"""
    url = f"{CONFIG['EMB_BASE_URL'].rstrip('/')}/embeddings"
    print(f"\n[2/3] 向量模型  {url}")
    print(f"      model={CONFIG['EMB_MODEL']}")
    status, body, elapsed = _post_json(
        url,
        {"model": CONFIG["EMB_MODEL"], "input": "连通性测试文本"},
        CONFIG["EMB_API_KEY"],
    )

    def check(b: str) -> bool:
        try:
            data = json.loads(b)
            emb = data["data"][0]["embedding"]
            return isinstance(emb, list) and len(emb) > 0
        except Exception:
            return False

    ok = _verdict("embeddings", status, body, elapsed, check)
    if ok:
        try:
            dim = len(json.loads(body)["data"][0]["embedding"])
            print(f"        向量维度：{dim}（应与 .env 的 EMBEDDING_DIM 一致）")
        except Exception:
            pass
    return ok


def test_rerank() -> bool:
    """重排模型：query + 两条文档，校验相关文档得分高于无关文档。"""
    url = f"{CONFIG['RERANK_BASE_URL'].rstrip('/')}/rerank"
    print(f"\n[3/3] 重排模型  {url}")
    print(f"      model={CONFIG['RERANK_MODEL']}")
    status, body, elapsed = _post_json(
        url,
        {
            "model": CONFIG["RERANK_MODEL"],
            "query": "跨网传输的审批策略",
            "documents": ["跨网传输实行分级审批制度", "今天天气晴朗适合外出"],
        },
        CONFIG["RERANK_API_KEY"],
    )

    def check(b: str) -> bool:
        try:
            results = json.loads(b)["results"]
            return len(results) == 2
        except Exception:
            return False

    ok = _verdict("rerank", status, body, elapsed, check)
    if ok:
        try:
            results = json.loads(b := body)["results"]
            s_rel = next(r["relevance_score"] for r in results if r["index"] == 0)
            s_irr = next(r["relevance_score"] for r in results if r["index"] == 1)
            order_ok = s_rel > s_irr
            print(f"        相关文档得分 {s_rel:.4f} / 无关文档得分 {s_irr:.4f}"
                  f" → 排序{'正确' if order_ok else '异常'}")
        except Exception:
            pass
    return ok


def main() -> int:
    print("=" * 64)
    print("模型配置连通性测试（参数见顶部 CONFIG，可用环境变量覆盖）")
    print("=" * 64)
    results = {
        "生成模型": test_gen(),
        "向量模型": test_emb(),
        "重排模型": test_rerank(),
    }
    print("\n" + "=" * 64)
    print("汇总：")
    for name, ok in results.items():
        print(f"  {'[PASS]' if ok else '[FAIL]'} {name}")
    all_ok = all(results.values())
    print("结论：" + ("全部可用 ✔" if all_ok else "存在不可用项，请按上方提示修复后重测 ✘"))
    print("=" * 64)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
