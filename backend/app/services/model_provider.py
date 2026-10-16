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

import httpx
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
        # gen 使用流式输出，单篇公文起草体量大（含思考链），留 300s 余量；
        # emb/rerank 输入短，分批调用后单批 180s 足够。
        _timeout = 300.0 if role == "gen" else 180.0
        self.client = AsyncOpenAI(base_url=base_url, api_key=self.api_key, timeout=_timeout)

    # ---- 向量化（批量）----
    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(model=self.model_name, input=texts)
        return [d.embedding for d in resp.data]

    # ---- 生成 ----
    async def generate(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2048,
        extra_body: dict | None = None,
    ) -> str:
        """非流式生成；extra_body 透传底层参数（如 Qwen3 的
        ``chat_template_kwargs={"enable_thinking": False}``——ReAct 决策
        调用需要纯 JSON 输出，关闭思考链避免 reasoning 干扰解析）。
        """
        kwargs: dict = {"extra_body": extra_body} if extra_body else {}
        resp = await self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    # ---- 流式生成（SSE 逐 token 输出，避免大草稿"假死/超时"）----
    async def generate_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ):
        """流式生成，异步产出 ``("content", text)`` 或 ``("reasoning", text)`` 片段。

        Qwen3 系列在 delta 中同时携带 ``reasoning_content``（思考链）与 ``content``
        （正式回答）；分别产出，便于前端区分"思考过程"与"正文"。

        max_tokens 默认从 1500 提到 4096（2026-10-16）：Qwen3 **思考链与正文共用
        同一个 max_tokens 预算**——思考链长时正文会被截断，表现为"答案说到一半
        戛然而止"。实测原 1500 下一个 300 字回答的思考链就吃掉大半预算，正文只剩
        157 字且明显断尾。深度推理链路的思考链尤其长（模型要复述材料、规划引用），
        预算过小必然截断。
        """
        stream = await self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                yield ("reasoning", rc)
            content = getattr(delta, "content", None)
            if content:
                yield ("content", content)

    # ---- 重排（在线，对 top-N 逐条打分）----
    async def rerank(self, query: str, docs: list[str], top_n: int | None = None) -> list[float]:
        """重排：调用 OpenAI 兼容的 /v1/rerank 原生端点（vLLM 风格）。

        原生 /v1/rerank 请求体：{model, query, documents[, top_n]}
        响应体：{results:[{index, document:{text}, relevance_score}]}
          - results 按**原始输入顺序**给出，需按 index 对齐回输入顺序。

        文档截断（rerank_doc_max_chars，2026-10-13 实测后增加）：
        内网 Qwen3-Reranker-0.6B 部署的最大上下文为 4096 tokens，存量文档
        存在「整篇 1 个 chunk」的长块（实测最长 5760 字，query+doc ≈ 5775
        tokens），直接投喂会被服务端 HTTP 400 确定性拒绝；且长文档打分
        耗时劣化严重（单个 3829 字文档 ~38s）。公文的相关性信息高度集中在
        标题与开头（文号、事由、总则），截断到前 N 字对重排判断影响可忽略，
        却能把 8 文档批次从「>40s 超时」压到 ~6.5s。截断仅改变送评文本，
        不改变文档数量，响应按 index 对齐的逻辑不受影响。

        超时（30s）：截断后空闲态 8 文档 ~6.5s；共享模型服务器偶发拥塞
        （串行处理，超时的客户端请求仍占用服务端）会劣化到 ~50s，30s 可
        覆盖绝大多数正常态；仍然失败则由检索层降级 RRF，不阻断主链路。
        """
        # 截断过长的文档，规避服务端 4096 tokens 上限与长文耗时劣化
        cap = self.settings.rerank_doc_max_chars
        clipped = [d[:cap] if len(d) > cap else d for d in docs]
        payload = {"model": self.model_name, "query": query, "documents": clipped}
        if top_n:
            payload["top_n"] = top_n

        # 单次调用，30s 超时；失败（超时/连接/协议错误）直接抛出，检索层降级 RRF。
        RERANK_TIMEOUT = 30.0
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
            if top_n:
                order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[
                    :top_n
                ]
                scores = [scores[i] for i in order]
            return scores
        except Exception as exc:
            # 连接/超时/协议错误（含长文 400）：如实抛出，交给检索层降级
            # （返回 RRF 顺序）。截断后正常不再触发 400，此处是最后防线。
            raise exc
