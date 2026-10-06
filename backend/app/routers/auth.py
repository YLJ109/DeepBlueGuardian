"""认证接口：登录、当前用户、修改密码、账号管理。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from ..config import settings
from ..deps import CurrentUser, DbSession, require_role
from ..models import AuditLog, User
from ..schemas import LoginRequest, TokenResponse, UserOut
from ..security import create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/login", response_model=TokenResponse, summary="账号登录")
def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    user = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    user.last_login = datetime.now().replace(microsecond=0)
    db.add(
        AuditLog(
            username=user.username,
            action="login",
            target="platform",
            detail={"role": user.role},
            ip=_client_ip(request),
        )
    )
    db.commit()
    token = create_token({"sub": user.username, "role": user.role, "name": user.display_name})
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_seconds,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="获取当前用户")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/password", summary="修改本账号密码")
def change_password(payload: dict, user: CurrentUser, db: DbSession) -> dict:
    old = payload.get("old_password", "")
    new = payload.get("new_password", "")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="新密码长度不得少于 8 位")
    if not verify_password(old, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    user.password_hash = hash_password(new)
    db.add(AuditLog(username=user.username, action="change_password", target=user.username))
    db.commit()
    return {"code": 0, "message": "密码已更新"}


@router.get("/users", summary="用户列表（管理员）")
def list_users(db: DbSession, _: User = Depends(require_role("admin"))) -> dict:
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    return {
        "code": 0,
        "data": [UserOut.model_validate(u).model_dump(mode="json") for u in users],
    }


@router.post("/users", summary="新增用户（管理员）")
def create_user(payload: dict, db: DbSession, admin: User = Depends(require_role("admin"))) -> dict:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="用户名必填，密码不少于 8 位")
    if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=username,
        display_name=str(payload.get("display_name") or username),
        role=str(payload.get("role") or "viewer"),
        phone=payload.get("phone"),
        region_scope=payload.get("region_scope"),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.add(AuditLog(username=admin.username, action="create_user", target=username, detail={"role": user.role}))
    db.commit()
    db.refresh(user)
    return {"code": 0, "data": UserOut.model_validate(user).model_dump(mode="json")}


@router.patch("/users/{user_id}", summary="更新用户状态与角色（管理员）")
def update_user(user_id: int, payload: dict, db: DbSession, admin: User = Depends(require_role("admin"))) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    for field in ("display_name", "role", "phone", "region_scope", "active"):
        if field in payload:
            setattr(user, field, payload[field])
    db.add(AuditLog(username=admin.username, action="update_user", target=user.username, detail=payload))
    db.commit()
    return {"code": 0, "data": UserOut.model_validate(user).model_dump(mode="json")}


@router.get("/audit", summary="操作审计日志（管理员）")
def audit_logs(db: DbSession, limit: int = 100, _: User = Depends(require_role("admin"))) -> dict:
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))).scalars().all()
    return {
        "code": 0,
        "data": [
            {
                "id": r.id,
                "username": r.username,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in rows
        ],
    }
