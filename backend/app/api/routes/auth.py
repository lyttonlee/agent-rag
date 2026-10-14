"""认证路由：/v1/auth。

四类接口：
1. **用户注册（运维台）**：POST /v1/auth/register——自然人自助注册控制台
   账号，成功直接下发 JWT（注册即登录）；角色固定 viewer（最低权限）。
2. **登录（用户）**：POST /v1/auth/login 换 JWT；GET /v1/auth/me 查当前用户。
3. **第三方注册（注册调用）**：POST /v1/auth/clients/register——
   第三方系统自助注册，返回 client_id + api_key（仅一次明文）。
   注册端点本身开放（未认证）：这是"注册调用"的入口，注册即拿凭证。
4. **客户端管理（admin）**：列表 / 禁用 / 启用。api_key 永不明文返回。

调用方在业务请求头携带：
- 用户（运维台）：Authorization: Bearer <JWT>
- 第三方：X-API-Key: sk-xxxx
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ApiClient, User
from app.db.session import get_db
from app.schemas.contracts import (
    ClientRegisterReq,
    ClientRegisterResp,
    ClientResp,
    LoginReq,
    LoginResp,
    RegisterReq,
    UserResp,
)
from app.services.auth import (
    Caller,
    create_access_token,
    ensure_admin_user,
    generate_api_key,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


def _user_resp(u: User) -> UserResp:
    return UserResp(
        id=u.id,
        username=u.username,
        display_name=u.display_name or "",
        role=u.role,
        enabled=u.enabled,
        last_login_at=u.last_login_at,
    )


def _client_resp(c: ApiClient) -> ClientResp:
    return ClientResp(
        client_id=c.client_id,
        name=c.name,
        key_prefix=c.key_prefix,
        scopes=c.scopes or [],
        max_clearance=c.max_clearance,
        enabled=c.enabled,
        expires_at=c.expires_at,
        created_by=c.created_by,
        last_used_at=c.last_used_at,
        created_at=c.created_at,
    )


@router.post("/login", response_model=LoginResp, summary="用户登录（换 JWT）")
async def login(req: LoginReq, db: AsyncSession = Depends(get_db)) -> LoginResp:
    """用户名 + 口令登录，成功返回 JWT（默认 12 小时有效）。"""
    user = (
        await db.execute(select(User).where(User.username == req.username.strip()))
    ).scalar_one_or_none()
    if user is None or not user.enabled or not verify_password(req.password, user.password_hash):
        # 不区分"账号不存在"与"口令错误"，防账号枚举
        raise HTTPException(status_code=401, detail="用户名或口令错误")
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return LoginResp(
        access_token=create_access_token(user),
        token_type="bearer",
        expires_in=settings.jwt_expire_hours * 3600,
        user=_user_resp(user),
    )


@router.post("/register", response_model=LoginResp, summary="运维台用户自助注册（注册即登录）")
async def register(req: RegisterReq, db: AsyncSession = Depends(get_db)) -> LoginResp:
    """自然人用户自助注册（用户 2026-10-14 需求：登录注册功能）。

    与第三方「注册调用」是两套体系：本端点创建的是**前端控制台登录账号**
    （users 表），注册成功直接下发 JWT，前端免去二次登录。

    安全边界（内网运维台定位，非互联网产品）：
    - 角色固定 viewer（最低权限）：看不见认证管理，管理操作一律做不了；
      提权/禁用由 admin 直接改库（users.role/enabled），本期不做用户管理页。
    - 用户名唯一，冲突返回 409（注册场景必须让用户知道重名了，不同于登录的
      防枚举策略）。
    - 口令下限 6 位由 schema 约束；哈希沿用 pbkdf2-sha256（auth.hash_password）。
    - 端点开放（无需认证）——否则未登录用户无法注册，与登录页可达性一致；
      滥用防护依赖内网边界 + admin 可禁用账号兜底。
    """
    username = req.username.strip()
    existing = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已被注册，请更换")
    user = User(
        username=username,
        password_hash=hash_password(req.password),
        display_name=req.display_name.strip() or username,
        role="viewer",  # 自助注册一律最低权限，提权走 admin 侧
        enabled=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return LoginResp(
        access_token=create_access_token(user),
        token_type="bearer",
        expires_in=settings.jwt_expire_hours * 3600,
        user=_user_resp(user),
    )


@router.get("/me", response_model=UserResp, summary="当前登录用户")
async def me(user: User = Depends(get_current_user)) -> UserResp:
    """凭 JWT 查询当前用户信息（前端启动时校验登录态用）。"""
    return _user_resp(user)


@router.post(
    "/clients/register",
    response_model=ClientRegisterResp,
    summary="第三方客户端注册（返回一次性 API Key）",
)
async def register_client(
    req: ClientRegisterReq, db: AsyncSession = Depends(get_db)
) -> ClientRegisterResp:
    """第三方系统自助注册，获取调用凭证。

    开放端点（无需认证）——注册即拿凭证，符合"注册调用"流程；
    滥用防护由 max_clearance/scopes 额度约束 + 管理端禁用兜底。
    """
    import secrets as _secrets

    key, key_hash, prefix = generate_api_key()
    client = ApiClient(
        client_id=f"cli-{_secrets.token_hex(8)}",
        name=req.name.strip() or "未命名客户端",
        api_key_hash=key_hash,
        key_prefix=prefix,
        scopes=req.scopes or ["chat", "retrieve"],
        max_clearance=req.max_clearance,
        created_by="self-register",
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    resp = ClientRegisterResp(**_client_resp(client).model_dump(), api_key=key)
    return resp


@router.get("/clients", response_model=list[ClientResp], summary="客户端列表（admin）")
async def list_clients(
    _: Caller = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[ClientResp]:
    """管理员查看全部第三方客户端（api_key 仅展示前缀）。"""
    rows = (await db.execute(select(ApiClient).order_by(ApiClient.created_at.desc()))).scalars().all()
    return [_client_resp(c) for c in rows]


@router.post("/clients/{client_id}/disable", response_model=ClientResp, summary="禁用客户端（admin）")
async def disable_client(
    client_id: str, _: Caller = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> ClientResp:
    """禁用后该客户端持有的 API Key 立即失效（应急止血用）。"""
    row = (
        await db.execute(select(ApiClient).where(ApiClient.client_id == client_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="客户端不存在")
    row.enabled = False
    await db.commit()
    await db.refresh(row)
    return _client_resp(row)


@router.post("/clients/{client_id}/enable", response_model=ClientResp, summary="启用客户端（admin）")
async def enable_client(
    client_id: str, _: Caller = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> ClientResp:
    """恢复被禁用的客户端。"""
    row = (
        await db.execute(select(ApiClient).where(ApiClient.client_id == client_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="客户端不存在")
    row.enabled = True
    await db.commit()
    await db.refresh(row)
    return _client_resp(row)


__all__ = ["router", "ensure_admin_user", "hash_password"]
