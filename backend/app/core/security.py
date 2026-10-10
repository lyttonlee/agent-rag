"""安全控制：密级过滤 + 权限下推。

设计铁律（详见《详细设计文档》）：
- 密级 / 权限过滤 **必须在检索阶段下推到数据库 WHERE**，
  涉密 / 越权内容在数据库层即被排除，**不进入模型上下文**。
- 我方不重认证终端用户，信任上游网关传入的 caller_scope + max_clearance；
  但过滤必须 **服务端强制执行**，不能依赖第三方"不问就不给"。

权限标签语义（我方制定的标准）：
  scope:<type>:<value>，type ∈ {dept, role, user, group}
可见判定：文档的 permission_tags 与调用方 scope 存在交集，且
          文档 security_level 的密级权重 ≤ 调用方 max_clearance 权重。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_

from app.core.config import SECURITY_WEIGHT, get_settings

# 权限标签前缀
SCOPE_PREFIX = "scope:"


def _allowed_levels(max_clearance: str) -> list[str]:
    """返回调用方可见的密级集合（权重 ≤ 上限）。"""
    caller_weight = SECURITY_WEIGHT.get(max_clearance, 0)
    return [lvl for lvl, w in SECURITY_WEIGHT.items() if w <= caller_weight]


def build_visibility_filter(
    max_clearance: str,
    caller_scope: list[str],
    *,
    security_level_col: Any,
    permission_tags_col: Any,
) -> ColumnElement[bool]:
    """构造"可见性"SQL 过滤表达式（密级 + 权限交集）。

    Args:
        max_clearance: 调用方最高可见密级（我方枚举值）。
        caller_scope:  调用方权限标签集合（scope:type:value）。
        security_level_col: 文档/切片表的 security_level 列。
        permission_tags_col: 文档/切片表的 permission_tags 列（数组）。

    Returns:
        可直接用于 query.where(...) 的 SQLAlchemy 布尔表达式。
    """
    # 1) 密级上限：仅当文档密级 ∈ 允许集合时可见
    clearance_ok = security_level_col.in_(_allowed_levels(max_clearance))

    # 2) 权限可见性：
    #    - 文档无任何权限标签（全局公开）→ 对所有调用方可见（只要密级符合）；
    #    - 文档带权限标签 → 仅当与调用方 caller_scope 任意相交时可见；
    #    - 调用方无权限上下文 → 仅允许"无权限约束"的文档。
    #    PostgreSQL 数组相交：permission_tags && ARRAY[...]
    tags_empty = func.coalesce(func.array_length(permission_tags_col, 1), 0) == 0
    if caller_scope:
        scope_ok = or_(
            tags_empty,
            permission_tags_col.op("&&")(list(caller_scope)),
        )
    else:
        scope_ok = tags_empty

    return and_(clearance_ok, scope_ok)


def redact_sensitive(text: str, sensitive_words: list[str]) -> tuple[str, list[str]]:
    """敏感词后处理（生成输出之后）。

    命中词以 ■ 掩码，并返回命中列表交由响应体 sensitive_hits 反馈第三方。
    主库治理在第三方；我方仅做执行与回报。

    Returns:
        (处理后文本, 命中词列表)
    """
    hits: list[str] = []
    result = text
    for w in sensitive_words:
        if w and w in result:
            hits.append(w)
            result = result.replace(w, "■" * len(w))
    return result, hits
