"""six-level security mapping (非密/内部/普通商密/核心商密/密级/机密)

Revision ID: 0015_security_level_six
Revises: 0014_template_ownership
Create Date: 2026-10-15

密级体系由四级扩为六级（用户 2026-10-15 需求）：
  非密(1.0) < 内部(2.0) < 普通商密(2.3) < 核心商密(2.7) < 密级(3.0) < 机密(4.0)

存量数据映射（序位一致 ⇒ 可见性关系完全不变）：
- 资源侧（documents/chunks/ingestion_jobs 的 security_level）：
  RESTRICTED(2) → COMMERCIAL(2.3)；SECRET(3) → TOP_SECRET(4)
- 额度侧（api_clients 的 max_clearance）：
  RESTRICTED(2) → CORE_COMMERCIAL(2.7)——旧"受限"调用方原本就看不到
  SECRET(3) 文档，映射后 2.7<4 依旧看不到，不会意外放行；
  SECRET(3) → TOP_SECRET(4)——admin 保持全量可见。

表清单说明（曾踩坑）：templates 表**没有** security_level 列（模板是
公共文种资源，不挂密级），users 表**没有** max_clearance 列（用户额度由
角色推导，见 services/auth.py cap_clearance）。两者不可列入本迁移。

WHERE 精确匹配旧值 ⇒ 天然幂等（重跑无副作用）；不影响已是新枚举的行。
回滚 = 反向 UPDATE（新值不可能被旧代码写入，回滚安全）。
"""

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0015_security_level_six"
down_revision: str | None = "0014_template_ownership"
branch_labels: str | None = None
depends_on: str | None = None

# 资源侧：security_level 列（documents/chunks/ingestion_jobs，均以模型元数据核实）
_RESOURCE_TABLES = ("documents", "chunks", "ingestion_jobs")
# 额度侧：max_clearance 列（映射不同于资源侧，见模块 docstring；仅 api_clients 落库）
_CALLER_TABLES = ("api_clients",)


def _has_column(table: str, column: str) -> bool:
    """目标表是否存在指定列（防 schema 漂移：缺列时跳过而非整链失败）。

    纯数据迁移对列缺失应当宽容——历史上 templates/users 就曾因没有对应
    列导致整条迁移链报错、版本号卡死。宁可跳过（无数据可迁）也不能
    让启动流程被一个历史表清单拖死。
    """
    insp = sa_inspect(op.get_bind())
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _remap(table: str, column: str, mapping: dict[str, str]) -> None:
    """按精确匹配做旧值→新值 UPDATE（幂等；无匹配行时为零操作）。"""
    if not _has_column(table, column):
        return
    for old, new in mapping.items():
        op.execute(f"UPDATE {table} SET {column}='{new}' WHERE {column}='{old}'")


def upgrade() -> None:
    for table in _RESOURCE_TABLES:
        _remap(table, "security_level", {
            "RESTRICTED": "COMMERCIAL",
            "SECRET": "TOP_SECRET",
        })
    for table in _CALLER_TABLES:
        _remap(table, "max_clearance", {
            "RESTRICTED": "CORE_COMMERCIAL",
            "SECRET": "TOP_SECRET",
        })


def downgrade() -> None:
    """反向映射（新枚举值不可能由旧代码产生，回滚无数据损失）。"""
    for table in _CALLER_TABLES:
        _remap(table, "max_clearance", {
            "CORE_COMMERCIAL": "RESTRICTED",
            "TOP_SECRET": "SECRET",
        })
    for table in _RESOURCE_TABLES:
        _remap(table, "security_level", {
            "COMMERCIAL": "RESTRICTED",
            "TOP_SECRET": "SECRET",
        })
