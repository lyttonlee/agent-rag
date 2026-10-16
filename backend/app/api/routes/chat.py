"""智能问答路由：/v1/chat（SSE 流式，多轮会话）。

与 /v1/generate（公文起草）的分工：
- /v1/chat：基于知识文档回答问题，支持多轮追问、会话历史；
- /v1/generate：按文种模板起草公文，同样支持会话（多轮修改）。

两者共用检索/生成/脱敏/审计基础设施；密级权限与空间过滤均在检索阶段下推。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.contracts import ChatRequest
from app.services.agent.react_config import load_react_config
from app.services.agent.react_prompt import export_prompts
from app.services.agent.react_tools import TOOL_DESCRIPTIONS, tool_lines
from app.services.audit import actor_from_request
from app.services.auth import Caller, get_caller
from app.services.chat import stream_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/stream", dependencies=[Depends(get_caller)])
async def chat_stream(
    req: ChatRequest, request: Request, caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """智能问答流式接口（SSE）。

    conversation_id 为空时自动创建会话；多轮上下文由后端自动拼接。
    Agent 模式（默认开）下先做意图路由再分发策略，SSE 事件增加
    intent/plan/step_*（老客户端忽略新事件即可兼容）。
    前端用 fetch + ReadableStream 消费，实时渲染；点「停止」时后端
    自动保存已生成部分为助手消息。

    深度推理（ReAct，2026-10-16）：请求 deep_reason（缺省跟配置中心，
    默认全开）时，问答类意图（确切回答/复杂任务）由编排层改写为
    deep_reason 意图并进入 TAO 循环，额外推送 react_start / analysis /
    react_round / thought / action / observation / react_end 事件，
    最终答案仍走既有 meta / token / done。显式 deep_reason=false 可
    完全切回普通问答。

    认证（2026-10-13）：双模认证（用户 JWT / 第三方 X-API-Key）；
    请求声明的 max_clearance 会被调用方额度压低（权限管控入口），
    第三方客户端还需具备 chat 能力域。
    空间所有权（2026-10-14）：请求/会话绑定的 space_ids 仅可纳入
    本人可见空间（默认公共 + 本人创建），他人空间 403。
    """
    if not caller.allows_scope("chat"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="该客户端无 chat 能力域权限")
    # 空间所有权：请求显式携带的空间必须可见（会话绑定在创建/更新时已校验）
    from app.api.routes.spaces import check_space_ids_visible

    await check_space_ids_visible(db, req.space_ids, caller)
    # 权限管控：请求密级不得超过调用方额度（密级过滤下推的入口）
    req.max_clearance = caller.cap_clearance(req.max_clearance)
    return StreamingResponse(
        stream_chat(db, req, actor=actor_from_request(request), caller=caller),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭代理缓冲，保证实时
        },
    )


@router.get("/react/prompts", summary="深度推理提示词清单（提示词管理面板）")
async def react_prompts(
    react_mode: str | None = Query(None, description="档位 fast/deep；缺省用配置中心默认档"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回深度推理各阶段提示词全文 + 可用工具清单。

    供前端「提示词管理」面板展示：运维/用户可直接查看"模型到底被如何
    指示"，无需翻代码。提示词为**运行时实际使用的那份**（工具清单与轮数
    已按当前档位注入），因此面板内容与真实请求完全一致。
    """
    cfg = await load_react_config(db)
    max_rounds, budget_ms = cfg.for_mode(react_mode)
    return {
        "mode": (react_mode or cfg.default_mode),
        "max_rounds": max_rounds,
        "total_budget_ms": budget_ms,
        "tools": [
            {"name": name, "description": desc} for name, desc in TOOL_DESCRIPTIONS.items()
        ],
        "prompts": export_prompts(tool_lines(), max_rounds),
    }
