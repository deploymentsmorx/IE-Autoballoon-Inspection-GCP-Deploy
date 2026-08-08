from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import current_user, require_admin
from ..db import AccessUpdate, EngineerCreate, LoginRequest, OrgCreate, create_token, verify_password
from .. import store

router = APIRouter(prefix="/api", tags=["auth-admin"])


@router.post("/auth/login")
def login(body: LoginRequest):
    user = store.find_user_by_login(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email/username or password")
    if not user.get("active") and user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Account inactive — ask super admin")
    token = create_token(user)
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "permissions": user.get("permissions") or {},
            "active": user.get("active", False),
        },
    }


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
        "permissions": user.get("permissions") or {},
        "active": user.get("active", False),
    }


@router.get("/admin/organizations")
def list_orgs(_: dict = Depends(require_admin)):
    return store.list_orgs()


@router.post("/admin/organizations")
def create_org(body: OrgCreate, _: dict = Depends(require_admin)):
    return store.create_org(body.name)


@router.get("/admin/engineers")
def list_engineers(_: dict = Depends(require_admin)):
    return store.list_engineers()


@router.post("/admin/engineers")
def add_engineer(body: EngineerCreate, _: dict = Depends(require_admin)):
    return store.add_engineer(body.full_name, body.email, body.org_id, body.password)


@router.put("/admin/access")
def update_access(body: AccessUpdate, _: dict = Depends(require_admin)):
    return store.update_access(body.user_id, body.active, body.read, body.write, body.delete)


@router.post("/admin/access/grant-full/{user_id}")
def grant_full(user_id: str, _: dict = Depends(require_admin)):
    return store.grant_full(user_id)
