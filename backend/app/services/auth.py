"""认证服务：用户口令哈希、JWT 签发/校验、第三方 API Key、双模认证依赖。

安全设计（用户 2026-10-13 需求：添加用户认证 + 第三方注册调用认证）：

1. **用户认证（运维台/控制台）**
   - 口令：pbkdf2_hmac-sha256，26 万次迭代，随机盐，存储格式
     ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``，不存明文；
   - 令牌：JWT（HS256），payload 携带 sub=user_id / role / exp，
     默认 12 小时过期（config.jwt_expire_hours 可调）；
   - 登录成功即刷新 last_login_at，便于审计。

2. **第三方注册调用认证（机器对机器）**
   - 注册：POST /v1/auth/clients/register 返回 client_id + api_key
     （明文**仅返回一次**，服务端只存 sha256 哈希，泄露也难逆推）；
   - 调用：请求头 ``X-API-Key: sk-xxxx``；
   - 额度：scopes（能力域白名单）+ max_clearance（可检索最高密级），
     与既有密级过滤下推联动（请求体 max_clearance 超过客户端额度被压低）。

3. **双模依赖 get_caller**：同一组 /v1 接口同时服务两种调用方——
   优先解析 Authorization: Bearer <JWT>（用户），否则解析 X-API-Key
   （第三方）。任一通过即放行，返回统一的 Caller 视图。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ApiClient, User
from app.db.session import get_db

settings = get_settings()

# pbkdf2 迭代次数：OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限为 60 万，
# 这里取 26 万在安全与内网 CPU 登录耗时之间取平衡（登录为低频操作）。
_PBKDF2_ITERATIONS = 260_000
_JWT_ALG = "HS256"


# =========================================================================
# 口令哈希
# =========================================================================

def hash_password(password: str) -> str:
    """生成口令哈希（pbkdf2_sha256$iterations$salt$hash）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令（恒定时间比较，防时序侧信道）。"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# =========================================================================
# JWT 签发 / 校验
# =========================================================================

def create_access_token(user: User) -> str:
    """签发用户访问令牌（JWT，HS256）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALG)


def decode_access_token(token: str) -> dict:
    """解码并校验令牌；非法/过期抛 jwt.PyJWTError。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALG])


# =========================================================================
# 第三方 API Key
# =========================================================================

def generate_api_key() -> tuple[str, str, str]:
    """生成 API Key。

    返回 (明文 key, sha256 哈希, 展示前缀)。明文仅注册时返回一次。
    """
    raw = secrets.token_urlsafe(32)
    key = f"sk-{raw}"
    return key, hash_api_key(key), key[:10]


def hash_api_key(key: str) -> str:
    """API Key 哈希（sha256；key 本身是高熵随机串，无需加盐）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# =========================================================================
# 统一调用方视图
# =========================================================================

@dataclass
class Caller:
    """一次请求的认证主体（用户或第三方客户端）。

    - kind：user / client；
    - max_clearance：可检索的最高密级（用户取角色映射，客户端取额度）；
    - scopes：第三方能力域白名单（用户为 None=不限制）。
    """

    kind: str
    id: str
    name: str
    role: str = "operator"
    max_clearance: str = "INTERNAL"
    scopes: list[str] | None = field(default=None)

    def allows_scope(self, scope: str) -> bool:
        """第三方客户端能力域校验（用户不限制）。"""
        if self.scopes is None:
            return True
        return "*" in self.scopes or scope in self.scopes

    def cap_clearance(self, requested: str) -> str:
        """把请求方声明的密级压低到主体额度之内（权限管控入口）。

        密级权重见 app.core.config.SECURITY_WEIGHT：取值越大密级越高。
        """
        from app.core.config import SECURITY_WEIGHT

        req_w = SECURITY_WEIGHT.get(requested, 1)
        cap_w = SECURITY_WEIGHT.get(self.max_clearance, 1)
        return requested if req_w <= cap_w else self.max_clearance


# =========================================================================
# FastAPI 依赖
# =========================================================================

async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析用户 JWT（Authorization: Bearer <token>）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="登录凭证无效或已过期")
    user = (
        await db.execute(select(User).where(User.id == payload.get("sub", "")))
    ).scalar_one_or_none()
    if user is None or not user.enabled:
        raise HTTPException(status_code=401, detail="账号不存在或已禁用")
    return user


async def get_api_client(
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiClient:
    """解析第三方 API Key（X-API-Key: sk-xxxx）。"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key 认证头")
    row = (
        await db.execute(
            select(ApiClient).where(ApiClient.api_key_hash == hash_api_key(x_api_key))
        )
    ).scalar_one_or_none()
    if row is None or not row.enabled:
        raise HTTPException(status_code=401, detail="API Key 无效或已禁用")
    if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="API Key 已过期")
    # 刷新最近使用时间（最佳努力，失败不影响主流程）
    try:
        row.last_used_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception:  # pragma: no cover
        await db.rollback()
    return row


async def get_caller(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Caller:
    """双模认证：用户 JWT 或第三方 API Key，任一通过即放行。

    所有 /v1 业务接口经此依赖保护；具体路由可再按 Caller.allows_scope
    做能力域细校验（如第三方仅开放 chat/retrieve）。
    """
    if authorization and authorization.lower().startswith("bearer "):
        user = await get_current_user(authorization=authorization, db=db)
        return Caller(
            kind="user",
            id=user.id,
            name=user.display_name or user.username,
            role=user.role,
            # 用户密级额度按角色映射：admin 全量，其余默认内部
            max_clearance="SECRET" if user.role == "admin" else "INTERNAL",
        )
    if x_api_key:
        client = await get_api_client(x_api_key=x_api_key, db=db)
        return Caller(
            kind="client",
            id=client.client_id,
            name=client.name,
            role="client",
            max_clearance=client.max_clearance,
            scopes=client.scopes,
        )
    raise HTTPException(status_code=401, detail="需要登录凭证（Bearer JWT）或 API Key（X-API-Key）")


async def require_admin(caller: Caller = Depends(get_caller)) -> Caller:
    """管理类操作（客户端管理等）要求 admin 用户。"""
    if caller.kind != "user" or caller.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return caller


async def ensure_admin_user(db: AsyncSession) -> User:
    """确保种子管理员存在（幂等，启动时调用）。

    账号口令来自配置（admin_username/admin_password，可用环境变量覆盖）；
    已存在则仅在被禁用时重新启用，不覆盖既有口令（避免重启把改过的密码冲掉）。
    """
    row = (
        await db.execute(select(User).where(User.username == settings.admin_username))
    ).scalar_one_or_none()
    if row is None:
        row = User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            display_name="系统管理员",
            role="admin",
            enabled=True,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    elif not row.enabled:
        row.enabled = True
        await db.commit()
    return row
