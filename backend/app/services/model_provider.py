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
        """重排：优先调用 OpenAI 兼容的 /v1/rerank 原生端点（vLLM 风格），
        失败则回退到 chat 兜底（部分仅暴露 chat 的部署方 / 历史 Mock）。

        原生 /v1/rerank 请求体：{model, query, documents[, top_n]}
        响应体：{results:[{index, document:{text}, relevance_score}]}
          - results 按**原始输入顺序**给出，需按 index 对齐回输入顺序。
        """
        payload = {"model": self.model_name, "query": query, "documents": docs}
        if top_n:
            payload["top_n"] = top_n

        # 优先调用原生 /v1/rerank（vLLM 风格）。内网 rerank 偶发冷启动/偏慢，
        # 放宽超时到 120s 并允许一次退避重试，避免误判为失败。
        last_exc: Exception | None = None
        for attempt in range(1, 3):
            try:
                import httpx

                async with httpx.AsyncClient(timeout=120.0) as client:
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
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(3)
        # 原生端点连续失败：对「连接/超时」类错误直接抛出（该端点本就是 rerank，
        # 不应再打 chat）；仅当端点确不存在时退回 chat 兜底。
        if isinstance(last_exc, (httpx.ConnectError, httpx.ReadTimeout, asyncio.TimeoutError)):
            raise last_exc  # 真实 rerank 端点慢/抖动：如实抛出，便于定位
        try:
            scores = []
            for d in docs:
                resp = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": "判断相关性，仅返回 0-1 的小数分数。",
                        },
                        {"role": "user", "content": f"查询：{query}\n文档：{d}"},
                    ],
                    temperature=0.0,
                    max_tokens=8,
                )
                try:
                    scores.append(float(resp.choices[0].message.content or "0"))
                except ValueError:
                    scores.append(0.0)
            return scores
        except Exception:
            raise last_exc

        if top_n:
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
            scores = [scores[i] for i in order]
        return scores
