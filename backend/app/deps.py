"""FastAPI 依赖：数据库会话、当前用户、角色校验。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .security import decode_token

DbSession = Annotated[Session, Depends(get_db)]

_ROLE_WEIGHT = {"viewer": 1, "farmer": 1, "technician": 2, "admin": 3}


def current_user(
    db: DbSession,
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少访问令牌")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    user = db.query(User).filter(User.username == payload.get("sub")).one_or_none()
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def optional_user(db: DbSession, authorization: str | None = Header(default=None)) -> User | None:
    """可选鉴权：大屏公开访问时允许匿名。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        return None
    return db.query(User).filter(User.username == payload.get("sub")).one_or_none()


OptionalUser = Annotated[User | None, Depends(optional_user)]


def require_role(minimum: str):
    """生成角色校验依赖。"""

    def _checker(user: CurrentUser) -> User:
        if _ROLE_WEIGHT.get(user.role, 0) < _ROLE_WEIGHT.get(minimum, 0):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号权限不足")
        return user

    return _checker


TechnicianUser = Annotated[User, Depends(require_role("technician"))]
AdminUser = Annotated[User, Depends(require_role("admin"))]
OperatorUser = Annotated[User, Depends(require_role("farmer"))]
