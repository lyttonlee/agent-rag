"""FastAPI 依赖。"""

from __future__ import annotations

from app.core.config import Settings, get_settings

__all__ = ["get_settings_dep"]


def get_settings_dep() -> Settings:
    """注入配置（与 core.config.get_settings 单例一致）。"""
    return get_settings()
