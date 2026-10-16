"""ReAct 工具注册表：knowledge_search（检索）/ doc_lookup（元数据）/ read_document（读原文）。

设计红线：
- 工具是**编排层对存量能力的薄封装**，不造新引擎——knowledge_search
  直接委托 retrieval.retrieve_enhanced（RRF 融合 + rerank + 查询改写 +
  父块注入 + **密级/权限/空间 WHERE 下推**全部原样保留）；
- **工具调用权完全交给模型**：是否检索、是否需要打开文档看全文、何时收尾，
  都由模型在 TAO 循环里自主决定，编排层不做"要不要查库"的路由；
- read_document 让模型能**打开原文核对**——检索命中的是片段（父块/摘要），
  遇到"这条到底规定了什么"必须看全文时由模型自己调用，避免凭摘要推断；
- doc_lookup 只返文档**元数据**（标题/文号/文种/切片数），不返正文；
- **网络搜索工具不提供**（私有化内网禁出网，涉密红线）；任意 SQL 直连
  不提供（越权风险）——预留注册位，合规工具未来可挂载；
- 工具参数来自模型 JSON，全部做存在性/长度校验；执行异常不外抛，
  转为 note 供模型换角度（主循环兜底）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import build_visibility_filter
from app.db.models import Chunk, Document
from app.schemas.contracts import Citation, RetrieveRequest
from app.services.agent.react_config import ReactConfig
from app.services.retrieval import retrieve_enhanced

logger = logging.getLogger(__name__)

TOOL_KNOWLEDGE_SEARCH = "knowledge_search"
TOOL_DOC_LOOKUP = "doc_lookup"
TOOL_READ_DOCUMENT = "read_document"

# read_document 的原文返回预算：总字符上限 + 单片截断 + 最多片数
# （实测教训：只返回前 4 片会让模型反复尝试读"下一页"浪费轮次——
#  "打开原文"应尽量给全，剩余部分用 note 说明，避免空转）
_READ_CHAR_BUDGET = 4000
_READ_CHARS_PER_CHUNK = 900
_READ_MAX_CHUNKS = 12

# 工具清单（含给模型看的描述；注册新工具 = 在此加一行 + 实现分支）
TOOL_DESCRIPTIONS: dict[str, str] = {
    TOOL_KNOWLEDGE_SEARCH: (
        "知识库语义+关键词混合检索，返回相关文档片段摘要。"
        "参数 query=你想查的具体点。适合先摸清有哪些相关材料"
    ),
    TOOL_DOC_LOOKUP: (
        "按标题/文号/文种列出候选文件（仅元数据）。"
        "参数 query=标题关键词或文号。适合先确定要读哪几份文件"
    ),
    TOOL_READ_DOCUMENT: (
        "打开某份文件/某一片的**原文全文**核对。"
        "参数 query=chunk_id 或文档标题。适合检索命中后仍需看清具体条款时"
    ),
}



def tool_names() -> list[str]:
    """可用工具名列表（SSE react_start 事件与 prompt 注入用）。"""
    return list(TOOL_DESCRIPTIONS)


def tool_lines() -> str:
    """prompt 用的工具清单文本（行列表）。"""
    return "\n".join(f"- {name}：{desc}" for name, desc in TOOL_DESCRIPTIONS.items())


@dataclass
class ToolResult:
    """一次工具执行的结果（含质量信号与提示语）。

    hits: 命中的引用（knowledge_search=切片引用，doc_lookup=文档摘要引用）；
    quality: 检索质量信号（向量距离/关键词命中/top1 相关度）；
    note: 给模型与前端看的附加说明（空结果/跳过/异常等）。
    """

    tool: str
    query: str
    hits: list[Citation] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    note: str = ""


async def execute_tool(
    db: AsyncSession,
    req,
    tool: str,
    args: dict,
    cfg: ReactConfig,
    caller=None,
) -> ToolResult:
    """执行一个工具（永不抛异常：失败转为 note，主循环可换角度重试）。"""
    query = str(args.get("query") or "").strip()
    try:
        if tool == TOOL_KNOWLEDGE_SEARCH:
            if not query:
                return ToolResult(tool=tool, query=query, note="缺少 query 参数，未执行检索")
            return await _knowledge_search(db, req, query, cfg)
        if tool == TOOL_DOC_LOOKUP:
            return await _doc_lookup(db, req, query, cfg, caller)
        if tool == TOOL_READ_DOCUMENT:
            if not query:
                return ToolResult(tool=tool, query=query, note="缺少 query 参数（chunk_id 或文档标题）")
            return await _read_document(db, req, query)
        return ToolResult(
            tool=tool, query=query,
            note=f"未知工具 {tool}，可用：{'、'.join(tool_names())}",
        )
    except Exception as exc:  # 工具异常不外抛：降级为观察提示
        logger.warning("ReAct 工具 %s 执行异常：%s", tool, exc)
        return ToolResult(tool=tool, query=query, note=f"工具执行异常：{type(exc).__name__}: {str(exc)[:120]}")


async def _knowledge_search(db: AsyncSession, req, query: str, cfg: ReactConfig) -> ToolResult:
    """知识库检索：复用 retrieve_enhanced（权限/密级/空间下推原样保留）。"""
    sub_req = RetrieveRequest(
        query=query,
        space_ids=getattr(req, "space_ids", None),
        doc_types=getattr(req, "doc_types", None),
        top_n=cfg.retrieve_top_n,
        caller_scope=getattr(req, "caller_scope", []) or [],
        max_clearance=getattr(req, "max_clearance", "INTERNAL"),
        rerank=getattr(req, "rerank", True),
        debug=False,
    )
    resp = await retrieve_enhanced(db, sub_req)
    hits: list[Citation] = list(resp.citations)
    quality = {
        "top_vector_distance": (
            round(float(resp.top_vector_distance), 4) if resp.top_vector_distance is not None else None
        ),
        "keyword_hits": resp.keyword_hits,
        "top1_score": round(float(hits[0].score), 4) if hits else None,
    }
    note = "" if hits else "未检索到相关内容，可更换关键词或换角度重试"
    return ToolResult(tool=TOOL_KNOWLEDGE_SEARCH, query=query, hits=hits, quality=quality, note=note)


async def _doc_lookup(
    db: AsyncSession, req, query: str, cfg: ReactConfig, caller
) -> ToolResult:
    """文档元数据查询：按标题/文号/文种定位文件（不返正文）。

    空间可见性与 list_documents 同构：请求限定的 space_ids 优先；
    无限定时取「调用方可见空间 + 无主文档（历史遗留）」；
    caller 缺失时（内部调用）仅按请求 space_ids 过滤，不加空间维度。
    """
    space_ids = getattr(req, "space_ids", None)
    if space_ids:
        space_cond = Document.space_id.in_(space_ids)
    elif caller is not None:
        from app.api.routes.spaces import visible_space_ids

        visible = await visible_space_ids(db, caller)
        space_cond = (
            Document.space_id.in_(visible) if visible else Document.space_id.is_(None)
        )
    else:
        space_cond = None

    stmt = select(Document).where(
        or_(space_cond, Document.space_id.is_(None)) if space_cond is not None else Document.space_id.is_(None)
    )
    # 仅已入库文档（PENDING/FAILED 的没有内容，列出来只会误导）
    stmt = stmt.where(Document.status == "INDEXED")
    if query:
        stmt = stmt.where(Document.title.ilike(f"%{query}%") | Document.doc_no.ilike(f"%{query}%"))
    doc_types = getattr(req, "doc_types", None)
    if doc_types:
        stmt = stmt.where(Document.doc_type.in_(doc_types))
    # 密级/权限下推：与检索链路同一过滤器（不同列名）
    vis_filter = build_visibility_filter(
        getattr(req, "max_clearance", "INTERNAL"),
        getattr(req, "caller_scope", []) or [],
        security_level_col=Document.security_level,
        permission_tags_col=Document.permission_tags,
    )
    stmt = stmt.where(vis_filter).order_by(Document.created_at.desc()).limit(10)
    rows = (await db.execute(stmt)).scalars().all()

    hits = [
        Citation(
            chunk_id=d.id,
            document_id=d.id,
            title=d.title,
            doc_no=d.doc_no,
            snippet=f"{d.doc_type} · {d.issuer or '发文机关未标注'} · {d.chunk_count} 片",
            score=0.0,
        )
        for d in rows
    ]
    note = "仅返文件元数据；需要正文内容请改用 knowledge_search" if hits else "未找到匹配文件"
    return ToolResult(
        tool=TOOL_DOC_LOOKUP, query=query, hits=hits,
        quality={"file_count": len(hits)}, note=note,
    )


def _chunk_visibility_clause(req) -> object:
    """切片层密级/权限 WHERE 条件（与检索链路同一过滤器）。

    切片表冗余了 security_level / permission_tags，因此**无需 JOIN documents**
    即可完成密级下推——read_document 与 retrieve_enhanced 的可见性口径一致。
    空间范围同样下推：请求限定 space_ids 时按空间过滤，未限定时不限空间
    （与 doc_lookup 内部"调用方可见空间"的处理不同：read_document 的空间
    条件由上层 `_ensure_conversation` 已收敛的 req.space_ids 承担，
    req.space_ids 为空表示"全部可见空间"，故不额外加空间条件）。
    """
    return build_visibility_filter(
        getattr(req, "max_clearance", "INTERNAL"),
        getattr(req, "caller_scope", []) or [],
        security_level_col=Chunk.security_level,
        permission_tags_col=Chunk.permission_tags,
    )


async def _read_document(db: AsyncSession, req, query: str) -> ToolResult:
    """打开原文：按 chunk_id 精确定位，或按标题模糊定位文档并读其前若干片。

    模型自主调用——检索命中片段后若需看清具体条款，用它核对原文。
    权限红线：切片层 WHERE 下推密级/权限 + 空间范围，越权内容永不返回。
    """
    vis = _chunk_visibility_clause(req)
    space_ids = getattr(req, "space_ids", None)

    # 情形一：query 看起来是 32 位 chunk_id → 直接读该片（含同文档相邻片）
    if _looks_like_id(query):
        row_stmt = select(Chunk).where(Chunk.id == query).where(vis)
        if space_ids:
            row_stmt = row_stmt.where(Chunk.space_id.in_(space_ids))
        row = (await db.execute(row_stmt)).scalar_one_or_none()
        if row is None:
            return ToolResult(
                tool=TOOL_READ_DOCUMENT, query=query,
                note="未找到该 chunk_id（可能不存在或无权限），请改用标题检索",
            )
        doc = (
            await db.execute(select(Document).where(Document.id == row.document_id))
        ).scalar_one_or_none()
        # 同文档相邻片：给模型一点上下文（seq±1）
        neigh_stmt = (
            select(Chunk)
            .where(Chunk.document_id == row.document_id, Chunk.seq.between(row.seq - 1, row.seq + 1))
            .where(vis)
        )
        if space_ids:
            neigh_stmt = neigh_stmt.where(Chunk.space_id.in_(space_ids))
        neigh = (await db.execute(neigh_stmt.order_by(Chunk.seq))).scalars().all()
        text = "\n\n".join(f"[第{c.seq}片] {c.content[:_READ_CHARS_PER_CHUNK]}" for c in neigh)
        title = doc.title if doc else ""
        return ToolResult(
            tool=TOOL_READ_DOCUMENT, query=query,
            hits=[Citation(chunk_id=row.id, document_id=row.document_id, title=title,
                           doc_no=doc.doc_no if doc else None, snippet=text[:4000], score=1.0)],
            quality={"chunk_count": len(neigh)},
            note="已打开原文（相邻片段一并返回），请据原文作答",
        )

    # 情形二：按标题/文号定位文档 → 读其前 N 片原文
    doc_stmt = select(Document).where(
        or_(Document.title.ilike(f"%{query}%"), Document.doc_no.ilike(f"%{query}%"))
    ).where(Document.status == "INDEXED")
    if space_ids:
        doc_stmt = doc_stmt.where(Document.space_id.in_(space_ids))
    docs = (await db.execute(doc_stmt.limit(3))).scalars().all()
    if not docs:
        return ToolResult(
            tool=TOOL_READ_DOCUMENT, query=query,
            note=f"未找到标题/文号包含「{query}」的文件，可换关键词或用 doc_lookup 先列候选",
        )
    all_hits: list[Citation] = []
    total_shown = 0
    total_all = 0
    truncated_docs: list[str] = []
    for d in docs:
        chunks_stmt = select(Chunk).where(Chunk.document_id == d.id).where(vis).order_by(Chunk.seq)
        if space_ids:
            chunks_stmt = chunks_stmt.where(Chunk.space_id.in_(space_ids))
        chunks = (await db.execute(chunks_stmt)).scalars().all()
        total_all += len(chunks)
        if not chunks:
            continue
        # 按字符预算逐片累加：能放多少放多少，放不下就记下来告诉模型"还有后续"
        parts: list[str] = []
        used = 0
        shown = 0
        for c in chunks[:_READ_MAX_CHUNKS]:
            piece = f"[第{c.seq}片] {c.content[:_READ_CHARS_PER_CHUNK]}"
            if used + len(piece) > _READ_CHAR_BUDGET and parts:
                break
            parts.append(piece)
            used += len(piece)
            shown += 1
        if shown < len(chunks):
            truncated_docs.append(f"《{d.title}》共 {len(chunks)} 片，本次返回前 {shown} 片")
        if not parts:
            continue
        total_shown += shown
        all_hits.append(
            Citation(
                chunk_id=chunks[0].id, document_id=d.id, title=d.title, doc_no=d.doc_no,
                snippet=f"{d.doc_type}｜原文（共 {len(chunks)} 片）：\n" + "\n\n".join(parts),
                score=1.0,
            )
        )
    note_bits = [f"已打开 {len(all_hits)} 份文件原文（返回 {total_shown} 片 / 文件共 {total_all} 片）"]
    if truncated_docs:
        # 明确告知还有后续，并给出可用的定位方式，避免模型反复空转试读
        note_bits.append("以下文件仍有后续内容未返回：" + "；".join(truncated_docs))
        note_bits.append("如需后续内容，请用 knowledge_search 检索该文件中的具体条款关键词")
    return ToolResult(
        tool=TOOL_READ_DOCUMENT, query=query, hits=all_hits,
        quality={"file_count": len(all_hits), "chunk_count": total_shown},
        note="；".join(note_bits) if all_hits else "找到文件但无可见正文（权限不足）",
    )


def _looks_like_id(s: str) -> bool:
    """判断字符串是否像 32 位 hex ID（chunk_id / document_id 均为 32 位 hex）。"""
    s = s.strip()
    return len(s) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in s)
