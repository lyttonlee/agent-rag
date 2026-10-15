"""迁移 0015 表清单与 ORM 元数据一致性（防回归）。

历史坑（2026-10-15 实踩）：初版迁移把 templates（无 security_level 列）
与 users（无 max_clearance 列）写进更新清单，PG 直接 UndefinedColumn，
alembic 事务回滚、版本号卡在 0014，启动只能走 dev create_all 兜底。

本测试把迁移声明的表清单与模型元数据双向对账：
- 正向：清单里的表必须真的存在且真的有对应列（否则迁移会整链失败）；
- 反向：凡有对应列的表都必须进清单（防止漏迁）。

运行：uv run pytest tests/test_migration_0015.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# 导入即注册：确保 Base.metadata 收录全部 ORM 表
from app.db import models as _models  # noqa: F401
from app.db.base import Base

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0015_security_level_six.py"
)


def _load_migration():
    """按文件路径加载迁移模块（versions 目录无 __init__.py，非包）。"""
    spec = importlib.util.spec_from_file_location("migration_0015_under_test", _MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resource_tables_have_security_level_column():
    """正向：资源侧清单每张表都必须有 security_level 列。"""
    m = _load_migration()
    tables = Base.metadata.tables
    assert m._RESOURCE_TABLES, "资源侧表清单不应为空"
    for table in m._RESOURCE_TABLES:
        assert table in tables, f"表 {table} 不在模型元数据中"
        assert "security_level" in tables[table].columns, (
            f"表 {table} 没有 security_level 列，迁移执行会 UndefinedColumn"
        )


def test_caller_tables_have_max_clearance_column():
    """正向：额度侧清单每张表都必须有 max_clearance 列。"""
    m = _load_migration()
    tables = Base.metadata.tables
    assert m._CALLER_TABLES, "额度侧表清单不应为空"
    for table in m._CALLER_TABLES:
        assert table in tables, f"表 {table} 不在模型元数据中"
        assert "max_clearance" in tables[table].columns, (
            f"表 {table} 没有 max_clearance 列，迁移执行会 UndefinedColumn"
        )


def test_no_column_table_is_missed():
    """反向：凡带 security_level / max_clearance 列的表都必须纳入清单。"""
    m = _load_migration()
    for name, table in Base.metadata.tables.items():
        if "security_level" in table.columns:
            assert name in m._RESOURCE_TABLES, (
                f"表 {name} 有 security_level 列但未纳入迁移 0015（存量数据漏迁）"
            )
        if "max_clearance" in table.columns:
            assert name in m._CALLER_TABLES, (
                f"表 {name} 有 max_clearance 列但未纳入迁移 0015（存量数据漏迁）"
            )


def test_migration_chain_head_is_0015():
    """迁移链头必须是 0015（防 down_revision 链断裂/新迁移没接上）。"""
    m = _load_migration()
    assert m.revision == "0015_security_level_six"
    assert m.down_revision == "0014_template_ownership"
