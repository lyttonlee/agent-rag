"""企业公文知识库 · 算力中心侧参考 Mock（部署方自有模型网关）。

本服务实现《接口文档》第 6 章「标准模型接入接口」（兼容 OpenAI 风格），
用于：
  1) 部署方算力中心团队据此对齐自己网关的请求/响应形态；
  2) 我方 ModelProvider(std) 在算力中心就绪前即可用它做端到端联调。

实现的端点：
  POST /v1/chat/completions   —— 生成
  POST /v1/embeddings         —— 向量（离线一次性）
  POST /v1/rerank             —— 重排（在线）

运行（backend 目录下）：
    uv run python mock_api/compute_center_mock.py
默认端口 8900；也可：uv run uvicorn mock_api.compute_center_mock:app --port 8900

对齐说明：部署方只要暴露上述三端点（或在其前加一层兼容网关），
我方即可零改代码接入自有模型（见 app/services/model_provider.py）。
"""

from __future__ import annotations

import hashlib
import math

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="Compute Center Std Gateway (Mock)",
    version="mock-1.0",
    description="部署方算力中心标准模型接口参考实现（OpenAI 风格）",
)

EMBED_DIM = 1024  # 须与 core.config.embedding_dim 一致


# =============================================================================
# 请求/响应模型
# =============================================================================
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.3
    max_tokens: int = 4096
    stream: bool = False


class EmbedRequest(BaseModel):
    model: str
    input: list[str]


class RerankRequest(BaseModel):
    model: str
    query: str
    documents: list[str]


# =============================================================================
# 工具：确定性伪向量（仅用于联调，不是真实语义向量）
# =============================================================================
def _pseudo_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = []
    for i in range(dim):
        # 用文本哈希 + 位置生成稳定但可区分的伪向量
        h = hashlib.sha256(f"{text}::{i}".encode("utf-8")).digest()
        val = (h[0] / 255.0) - 0.5 + math.sin(i * 0.01) * 0.05
        vec.append(round(val, 6))
    return vec


# =============================================================================
# Chat Completions
# =============================================================================
@app.post("/v1/chat/completions")
async def chat(req: ChatRequest) -> dict:
    last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    answer = (
        f"[算力中心 Mock 回复] 已收到关于「{last_user[:30]}」的请求，"
        f"模型={req.model}。此为参考网关的回包，用于验证接入链路，非真实推理结果。"
    )
    return {
        "id": "cmpl-mock-001",
        "object": "chat.completion",
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# =============================================================================
# Embeddings
# =============================================================================
@app.post("/v1/embeddings")
async def embed(req: EmbedRequest) -> dict:
    data = [
        {"index": i, "embedding": _pseudo_embed(text)}
        for i, text in enumerate(req.input)
    ]
    return {"object": "list", "data": data, "model": req.model}


# =============================================================================
# Rerank
# =============================================================================
@app.post("/v1/rerank")
async def rerank(req: RerankRequest) -> dict:
    # 伪重排：与 query 共享字符越多，分数越高（仅联调用）
    q_chars = set(req.query)
    results = []
    for i, doc in enumerate(req.documents):
        overlap = len(q_chars & set(doc)) / max(len(q_chars), 1)
        score = round(0.5 + 0.5 * overlap, 4)
        results.append({"index": i, "score": score})
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"model": req.model, "results": results}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "compute-center-mock"}


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8900, log_level="info")
