from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_user, require_admin
from app.core.config import settings
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


def _user_payload(user: dict, customer: dict | None = None) -> dict:
    payload = {
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
    if customer is not None:
        payload["customer"] = customer
    return payload


@router.post("/api/auth/login")
def login(body: LoginRequest):
    user = auth_service.find_user_by_login(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email/username or password")
    product_code = (body.product_code or "").strip().lower()
    customer = None
    if product_code:
        if product_code != "costsheet":
            raise HTTPException(status_code=403, detail="This login is not available for that product")
        customer = license_service.verify_user_product(user, product_code)
    if settings.costsheet_admin_portal_only and user.get("role") != "super_admin" and product_code != "costsheet":
        raise HTTPException(status_code=403, detail="Cost Sheet administrator access only")
    if not user.get("active") and user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Account inactive — ask super admin")
    # Desktop EXE sends device_id/hostname — must match the device this
    # account's license was activated on. Web logins omit these and skip it.
    if body.device_id and user.get("customer_id"):
        license_service.verify_device_for_login(
            user["customer_id"], body.device_id, body.hostname or ""
        )
    token = create_token(user)
    return {"token": token, "user": _user_payload(user, customer)}


@router.get("/api/auth/me")
def me(
    product_code: str | None = None,
    required_permission: str = "read",
    user: dict = Depends(current_user),
):
    customer = None
    if product_code:
        customer = license_service.verify_user_product(user, product_code, required_permission)
    return _user_payload(user, customer)


@router.post("/api/auth/change-password")
def change_password(body: ChangePasswordRequest, user: dict = Depends(current_user)):
    return auth_service.change_password(user["id"], body.current_password, body.new_password)


@router.get("/api/auth/device-status")
def device_status(device_id: str, product_code: str | None = None):
    """Lets the desktop app re-confirm activation after a reinstall wipes its local flag."""
    effective_product = (
        "costsheet"
        if settings.costsheet_admin_portal_only
        else (product_code or "autoballoon")
    )
    return license_service.device_status(device_id, product_code=effective_product)


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
        product_code=(
            "costsheet"
            if settings.costsheet_admin_portal_only
            else body.product_code
        ),
    )
