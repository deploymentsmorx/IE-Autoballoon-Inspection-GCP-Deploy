from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_user, require_admin
from app.core.security import create_token, verify_password
from app.schemas import (
    AccessUpdate,
    ChangePasswordRequest,
    EngineerCreate,
    LicenseActivateRequest,
    LoginRequest,
    OrgCreate,
)
from app.services import auth_service, license_service

router = APIRouter(tags=["auth"])


def _user_payload(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
        "permissions": user.get("permissions") or {},
        "active": user.get("active", False),
        "must_change_password": bool(user.get("must_change_password")),
        "customer_id": user.get("customer_id"),
    }


@router.post("/api/auth/login")
def login(body: LoginRequest):
    user = auth_service.find_user_by_login(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email/username or password")
    if not user.get("active") and user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Account inactive — ask super admin")
    # Desktop EXE sends device_id/hostname — must match the device this
    # account's license was activated on. Web logins omit these and skip it.
    if body.device_id and user.get("customer_id"):
        license_service.verify_device_for_login(
            user["customer_id"], body.device_id, body.hostname or ""
        )
    token = create_token(user)
    return {"token": token, "user": _user_payload(user)}


@router.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return _user_payload(user)


@router.post("/api/auth/change-password")
def change_password(body: ChangePasswordRequest, user: dict = Depends(current_user)):
    return auth_service.change_password(user["id"], body.current_password, body.new_password)


@router.get("/api/auth/device-status")
def device_status(device_id: str):
    """Lets the desktop app re-confirm activation after a reinstall wipes its local flag."""
    return license_service.device_status(device_id)


@router.post("/api/auth/activate")
def activate_license(body: LicenseActivateRequest):
    """First-launch EXE activation: install password + license + user + device details."""
    return license_service.activate_installation(
        license_key=body.license_key,
        username=body.username,
        password=body.password,
        device_id=body.device_id,
        hostname=body.hostname,
        install_password=body.install_password,
    )
