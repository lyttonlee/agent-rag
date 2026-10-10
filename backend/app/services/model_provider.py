"""模型 Provider 抽象（local / cloud / std 三类）。

设计（详见《详细设计文档》第 6 章）：
- 我方以 **OpenAI 兼容风格** 统一对接三类后端：
    local : 我方默认 Qwen3 栈（vLLM 内网部署）
    cloud : 开发期云端 API（生产环境启动自检拒绝）
    std   : 部署方自有算力中心（暴露兼容端点即可，零改我方代码）
- 切换仅是配置项，业务代码不感知。
"""

from __future__ import annotations

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

        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY")

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
        # vLLM / 兼容端点通常提供 /rerank；此处以 chat 兜底实现打分语义。
        # 若部署方提供原生 rerank 端点，可在此分支调用。
        scores: list[float] = []
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
        if top_n:
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
            scores = [scores[i] for i in order]
        return scores
