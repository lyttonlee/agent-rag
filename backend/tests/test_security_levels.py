"""六级密级体系单测（2026-10-15 用户需求：非密/内部/普通商密/核心商密/密级/机密）。

覆盖：
- normalize_security_level：规范代码 / 历史四级代码 / 中文名 / 大小写 / 脏值；
- security_weight：全序关系 + fail-closed（认不出按最低权重）；
- _allowed_levels：各额度可见集合（含"内部额度看不到商密"等关键断言）；
- Caller.cap_clearance：请求声明被额度压低 + 历史代码/中文名兼容；
- build_visibility_filter：SQL 可编译（密级条件含六级枚举）。

运行：uv run pytest tests/test_security_levels.py -v
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.core.config import (
    SECURITY_LEVEL_LABELS,
    SECURITY_WEIGHT,
    normalize_security_level,
    security_weight,
)
from app.core.security import _allowed_levels, build_visibility_filter
from app.db.models import Document
from app.services.auth import Caller

# ---- 归一化 ----


def test_normalize_canonical_codes():
    """规范代码原样返回（大小写不敏感）。"""
    for code in SECURITY_WEIGHT:
        assert normalize_security_level(code) == code
        assert normalize_security_level(code.lower()) == code


def test_normalize_legacy_four_levels():
    """历史四级代码映射到六级（序位一致）。"""
    assert normalize_security_level("PUBLIC") == "PUBLIC"
    assert normalize_security_level("INTERNAL") == "INTERNAL"
    assert normalize_security_level("RESTRICTED") == "COMMERCIAL"
    assert normalize_security_level("SECRET") == "TOP_SECRET"


def test_normalize_chinese_names():
    """中文名直传归一（第三方按自身习惯传"机密/商密"）。"""
    assert normalize_security_level("非密") == "PUBLIC"
    assert normalize_security_level("内部") == "INTERNAL"
    assert normalize_security_level("普通商密") == "COMMERCIAL"
    assert normalize_security_level("商密") == "COMMERCIAL"
    assert normalize_security_level("核心商密") == "CORE_COMMERCIAL"
    assert normalize_security_level("密级") == "CLASSIFIED"
    assert normalize_security_level("机密") == "TOP_SECRET"


def test_normalize_garbage_falls_back():
    """无法识别回落 default（空值/None/脏串）。"""
    assert normalize_security_level(None) == "INTERNAL"
    assert normalize_security_level("") == "INTERNAL"
    assert normalize_security_level("绝密") == "INTERNAL"  # 超出体系 → 缺省
    assert normalize_security_level("绝密", default="PUBLIC") == "PUBLIC"


# ---- 权重与可见集合 ----


def test_weight_total_order():
    """六级权重严格全序（非整数权重落在内部与密级之间）。"""
    order = ["PUBLIC", "INTERNAL", "COMMERCIAL", "CORE_COMMERCIAL", "CLASSIFIED", "TOP_SECRET"]
    weights = [SECURITY_WEIGHT[k] for k in order]
    assert weights == sorted(weights)
    assert len(set(weights)) == 6
    assert SECURITY_WEIGHT["COMMERCIAL"] == 2.3
    assert SECURITY_WEIGHT["CORE_COMMERCIAL"] == 2.7


def test_weight_fail_closed():
    """认不出的密级按最低权重处理（不会越权放行）。"""
    assert security_weight("不存在的密级") == SECURITY_WEIGHT["PUBLIC"]
    assert security_weight(None) == SECURITY_WEIGHT["PUBLIC"]
    # 历史代码仍按映射后权重
    assert security_weight("SECRET") == SECURITY_WEIGHT["TOP_SECRET"]
    assert security_weight("RESTRICTED") == SECURITY_WEIGHT["COMMERCIAL"]


def test_allowed_levels_per_clearance():
    """各额度可见集合：内部额度看不到商密（本体系的关键分层）。"""
    assert set(_allowed_levels("PUBLIC")) == {"PUBLIC"}
    assert set(_allowed_levels("INTERNAL")) == {"PUBLIC", "INTERNAL"}
    assert set(_allowed_levels("COMMERCIAL")) == {"PUBLIC", "INTERNAL", "COMMERCIAL"}
    assert set(_allowed_levels("CORE_COMMERCIAL")) == {
        "PUBLIC", "INTERNAL", "COMMERCIAL", "CORE_COMMERCIAL",
    }
    assert set(_allowed_levels("TOP_SECRET")) == set(SECURITY_WEIGHT)
    # 历史代码额度（旧客户端 JWT 未刷新）不丢可见性
    assert set(_allowed_levels("SECRET")) == set(SECURITY_WEIGHT)


def test_labels_cover_all_levels():
    """中文展示名全覆盖（前端下拉单一事实源）。"""
    assert set(SECURITY_LEVEL_LABELS) == set(SECURITY_WEIGHT)
    assert SECURITY_LEVEL_LABELS["CORE_COMMERCIAL"] == "核心商密"


# ---- 调用方额度压低 ----


def test_cap_clearance_legacy_and_chinese():
    """cap_clearance 兼容历史代码/中文名，并归一化回传。"""
    user = Caller(kind="user", id="u1", name="测试", role="viewer", max_clearance="INTERNAL")
    # 声明机密（中文/历史代码）→ 压到 INTERNAL
    assert user.cap_clearance("机密") == "INTERNAL"
    assert user.cap_clearance("SECRET") == "INTERNAL"
    # 声明内部 → 原样放行（归一后）
    assert user.cap_clearance("内部") == "INTERNAL"

    admin = Caller(kind="user", id="u2", name="管理员", role="admin", max_clearance="TOP_SECRET")
    assert admin.cap_clearance("机密") == "TOP_SECRET"
    assert admin.cap_clearance("COMMERCIAL") == "COMMERCIAL"


# ---- 可见性过滤 SQL ----


def test_visibility_filter_compiles_with_six_levels():
    """过滤表达式可按 PG 方言编译，且密级条件覆盖六级。"""
    expr = build_visibility_filter(
        "INTERNAL", ["scope:dept:总部"],
        security_level_col=Document.security_level,
        permission_tags_col=Document.permission_tags,
    )
    sql = str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    # 内部额度 → IN ('PUBLIC', 'INTERNAL')，且不含商密/密级/机密
    assert "'PUBLIC'" in sql and "'INTERNAL'" in sql
    assert "COMMERCIAL" not in sql
    assert "TOP_SECRET" not in sql
