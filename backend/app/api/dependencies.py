"""FastAPI auth dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token
from app.services import auth_service

security = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    token: str | None = Query(None, description="JWT for <img>/download when Bearer header is unavailable"),
) -> dict:
    raw = (creds.credentials if creds and creds.credentials else None) or token or request.query_params.get(
        "access_token"
    )
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(raw)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e
    user = auth_service.get_user(payload.get("sub"))
    if not user or (not user.get("active") and user.get("role") != "super_admin"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive or missing")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")
    return user


def require_perm(perm: str):
    def _inner(user: dict = Depends(current_user)) -> dict:
        if user.get("role") == "super_admin":
            return user
        perms = user.get("permissions") or {}
        if not user.get("active") or not perms.get(perm):
            raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
        return user

    return _inner
