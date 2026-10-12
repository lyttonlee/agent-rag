"""模型 Provider 抽象（local / cloud / std 三类）。

设计（详见《详细设计文档》第 6 章）：
- 我方以 **OpenAI 兼容风格** 统一对接三类后端：
    local : 我方默认 Qwen3 栈（vLLM 内网部署）
    cloud : 开发期云端 API（生产环境启动自检拒绝）
    std   : 部署方自有算力中心（暴露兼容端点即可，零改我方代码）
- 切换仅是配置项，业务代码不感知。
"""

from __future__ import annotations

import asyncio
import httpx
from typing import Literal

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings


class ModelProvider:
    """统一的模型接入门面。

    实例化时指定角色（gen / emb / rerank），按配置构造对应的 OpenAI 客户端。
    std 模式下 base_url 由部署方提供（内网地址），我方仅消费。
    """

    def __init__(
        self,
        role: Literal["gen", "emb", "rerank"],
        provider_type: Literal["local", "cloud", "std"] = "local",
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.role = role
        self.provider_type = provider_type

        if role == "gen":
            base_url = base_url or self.settings.model_gen_base_url
            api_key = api_key or self.settings.model_gen_api_key
            self.model_name = model_name or self.settings.model_gen_name
        elif role == "emb":
            base_url = base_url or self.settings.model_emb_base_url
            api_key = api_key or self.settings.model_emb_api_key
            self.model_name = model_name or self.settings.model_emb_name
        else:  # rerank
            base_url = base_url or self.settings.model_rerank_base_url
            api_key = api_key or self.settings.model_rerank_api_key
            self.model_name = model_name or self.settings.model_rerank_name

        # 涉密不出网：生产环境拒绝云端 Provider
        if (
            self.settings.forbid_cloud_in_production
            and self.settings.environment == "production"
            and self.provider_type == "cloud"
        ):
            raise RuntimeError(
                "生产环境禁止 cloud Provider（涉密不出网）。请切换为 local 或 std。"
            )

        # 保存基础连接信息，供 rerank 原生端点调用（非 chat 兜底）
        self.base_url = base_url
        self.api_key = api_key or "EMPTY"
        # 超时保护：内网模型偶发无响应时快速失败，避免整批入库挂死。
        # gen 留 120s 余量；emb/rerank 输入短，分批调用后单批 180s 足够。
        _timeout = 120.0 if role == "gen" else 180.0
        self.client = AsyncOpenAI(base_url=base_url, api_key=self.api_key, timeout=_timeout)

    # ---- 向量化（批量）----
    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(model=self.model_name, input=texts)
        return [d.embedding for d in resp.data]

    # ---- 生成 ----
    async def generate(
        self, messages: list[dict], temperature: float = 0.3, max_tokens: int = 2048
    ) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    # ---- 重排（在线，对 top-N 逐条打分）----
    async def rerank(self, query: str, docs: list[str], top_n: int | None = None) -> list[float]:
        """重排：调用 OpenAI 兼容的 /v1/rerank 原生端点（vLLM 风格）。

        原生 /v1/rerank 请求体：{model, query, documents[, top_n]}
        响应体：{results:[{index, document:{text}, relevance_score}]}
          - results 按**原始输入顺序**给出，需按 index 对齐回输入顺序。

        约定：rerank 端点应在 15s 内返回；超时或连接/协议错误时**抛出**，由检索
        层捕获并降级为 RRF 排序（不阻断主链路）。用户侧已确认 rerank 部署后
        「15s 超时则降级」的诉求。
        """
        payload = {"model": self.model_name, "query": query, "documents": docs}
        if top_n:
            payload["top_n"] = top_n

        # 单次调用，15s 超时；失败（超时/连接/协议错误）直接抛出，检索层降级 RRF。
        RERANK_TIMEOUT = 15.0
        try:
            async with httpx.AsyncClient(timeout=RERANK_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/rerank",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
            results = data.get("results", [])
            # 按 index 对齐回输入顺序：缺失项记 0.0
            score_map = {
                int(r["index"]): float(r.get("relevance_score", 0.0)) for r in results
            }
            scores = [score_map.get(i, 0.0) for i in range(len(docs))]
            return scores
        except Exception as exc:
            # 连接/超时/协议错误：如实抛出，交给检索层降级（返回 RRF 顺序）。
            raise exc

        if top_n:
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
            scores = [scores[i] for i in order]
        return scores
