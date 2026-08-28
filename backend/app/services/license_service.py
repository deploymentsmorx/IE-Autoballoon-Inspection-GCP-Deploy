"""License activation + device binding."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.core import database as dbmod
from app.core.security import create_token, verify_password
from app.models import ApprovedDevice, Customer, Installation, User
from app.services import auth_service


def device_status(device_id: str) -> dict:
    """
    Server-side truth for "is this machine already activated?", keyed by the
    Windows MachineGuid (stable across app reinstalls/updates, unlike the
    browser localStorage flag the desktop app otherwise relies on).
    """
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return {"activated": False}

    device_id = device_id.strip().upper()
    if not device_id:
        return {"activated": False}

    with dbmod.SessionLocal() as db:
        inst = db.scalar(
            select(Installation).where(
                Installation.device_id == device_id,
                Installation.status == "Active",
            )
        )
        if inst is None:
            return {"activated": False}
        customer = db.get(Customer, inst.customer_id)
        if customer is None or customer.status != "Active":
            return {"activated": False}
        return {
            "activated": True,
            "license_key": customer.license_key,
            "customer_id": customer.customer_id,
            "installation_id": inst.installation_id,
        }


def verify_device_for_login(customer_id: str, device_id: str, hostname: str) -> None:
    """
    Enforce that a desktop login comes from the same device_id + hostname
    recorded when this customer's license was activated. Raises 403 on any
    mismatch or missing/inactive installation.
    """
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (Neon) unavailable")

    device_id = device_id.strip().upper()
    hostname = hostname.strip()

    with dbmod.SessionLocal() as db:
        inst = db.scalar(
            select(Installation).where(
                Installation.customer_id == customer_id,
                Installation.status == "Active",
            )
        )
        if inst is None:
            raise HTTPException(
                status_code=403,
                detail="No active installation registered for this account — activate on this device first",
            )
        if inst.device_id != device_id or inst.hostname.strip().lower() != hostname.lower():
            raise HTTPException(
                status_code=403,
                detail="This device does not match the registered installation for this account",
            )


def activate_installation(
    *,
    license_key: str,
    username: str,
    password: str,
    device_id: str,
    hostname: str = "",
    install_password: str = "",
) -> dict:
    """
    First-launch EXE flow:
      install password + license + user + admin-provided device_id/hostname → bind → JWT
    """
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (Neon) unavailable")

    license_key = license_key.strip()
    device_id = device_id.strip().upper()
    hostname = hostname.strip()
    if not license_key or not device_id or not hostname:
        raise HTTPException(
            status_code=400,
            detail="license_key, device_id, and hostname are required",
        )

    user = auth_service.find_user_by_login(username)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.get("active") and user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Account inactive")

    with dbmod.SessionLocal() as db:
        customer = db.scalar(select(Customer).where(Customer.license_key == license_key))
        if customer is None or customer.status != "Active":
            raise HTTPException(status_code=403, detail="Invalid or inactive license")

        if customer.install_password_hash:
            if not install_password or not verify_password(install_password, customer.install_password_hash):
                raise HTTPException(status_code=403, detail="Invalid install password")

        if customer.require_device_approval:
            approved = db.scalar(
                select(ApprovedDevice).where(
                    ApprovedDevice.customer_id == customer.customer_id,
                    ApprovedDevice.device_id == device_id,
                )
            )
            if approved is None:
                raise HTTPException(
                    status_code=403,
                    detail="Device not approved — contact SmorX admin",
                )
            if approved.hostname.strip().lower() != hostname.lower():
                raise HTTPException(status_code=403, detail="Hostname does not match admin record")
            if approved.status == "Revoked":
                raise HTTPException(status_code=403, detail="Device approval revoked")

        other = db.scalar(
            select(Installation).where(
                Installation.customer_id == customer.customer_id,
                Installation.status == "Active",
                Installation.device_id != device_id,
            )
        )
        if other is not None:
            raise HTTPException(
                status_code=403,
                detail="License already registered to another device",
            )

        inst = db.scalar(
            select(Installation).where(
                Installation.customer_id == customer.customer_id,
                Installation.device_id == device_id,
            )
        )
        now = datetime.now(timezone.utc)
        if inst is None:
            inst = Installation(
                installation_id=f"INST-{secrets.token_hex(4).upper()}",
                customer_id=customer.customer_id,
                device_id=device_id,
                hostname=hostname,
                status="Active",
                activated_at=now,
                last_seen_at=now,
            )
            db.add(inst)
        else:
            if inst.status != "Active":
                raise HTTPException(status_code=403, detail="Installation blocked")
            inst.hostname = hostname
            inst.last_seen_at = now
            if inst.activated_at is None:
                inst.activated_at = now

        if customer.require_device_approval:
            approved = db.scalar(
                select(ApprovedDevice).where(
                    ApprovedDevice.customer_id == customer.customer_id,
                    ApprovedDevice.device_id == device_id,
                )
            )
            if approved is not None:
                approved.status = "Active"
                approved.activated_at = now

        db_user = db.get(User, user["id"])
        if db_user is not None:
            if not db_user.customer_id:
                db_user.customer_id = customer.customer_id

        db.commit()
        db.refresh(inst)
        token = create_token(user)
        return {
            "token": token,
            "customer": {
                "customer_id": customer.customer_id,
                "organization": customer.organization,
                "license_key": customer.license_key,
                "status": customer.status,
            },
            "installation": {
                "installation_id": inst.installation_id,
                "device_id": inst.device_id,
                "hostname": inst.hostname or "",
                "status": inst.status,
                "activated_at": inst.activated_at.isoformat() if inst.activated_at else "",
            },
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "must_change_password": bool(user.get("must_change_password")),
            },
        }
