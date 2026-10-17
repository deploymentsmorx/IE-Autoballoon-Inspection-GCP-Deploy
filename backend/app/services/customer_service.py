"""Customer, license, and pre-approved device management."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from app.core import database as dbmod
from app.core.security import hash_password
from app.models import ApprovedDevice, Customer, Installation, Job, Organization, User

VALID_PRODUCTS = frozenset({"autoballoon", "costsheet"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_product(product_code: str | None) -> str:
    code = (product_code or "autoballoon").strip().lower()
    if code not in VALID_PRODUCTS:
        raise HTTPException(
            status_code=400,
            detail=f"product_code must be one of: {', '.join(sorted(VALID_PRODUCTS))}",
        )
    return code


def _customer_out(db, c: Customer) -> dict:
    tenant_id = None
    if c.org_id:
        org = db.get(Organization, c.org_id)
        if org:
            tenant_id = org.tenant_id
    approved = db.scalar(
        select(func.count()).select_from(ApprovedDevice).where(
            ApprovedDevice.customer_id == c.customer_id
        )
    )
    installs = db.scalar(
        select(func.count()).select_from(Installation).where(
            Installation.customer_id == c.customer_id,
            Installation.status == "Active",
        )
    )
    return {
        "customer_id": c.customer_id,
        "organization": c.organization,
        "license_key": c.license_key,
        "status": c.status,
        "org_id": c.org_id,
        "tenant_id": tenant_id,
        "product_code": getattr(c, "product_code", None) or "autoballoon",
        "require_device_approval": bool(c.require_device_approval),
        "has_install_password": bool(c.install_password_hash),
        "approved_devices": int(approved or 0),
        "active_installations": int(installs or 0),
    }


def list_customers() -> list[dict]:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    with dbmod.SessionLocal() as db:
        rows = db.scalars(select(Customer).order_by(Customer.organization)).all()
        return [_customer_out(db, c) for c in rows]


def create_customer(
    *,
    organization: str,
    license_key: str = "",
    org_id: str | None = None,
    install_password: str = "",
    require_device_approval: bool = True,
    product_code: str = "autoballoon",
) -> dict:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    organization = organization.strip()
    if not organization:
        raise HTTPException(status_code=400, detail="organization required")

    product_code = _normalize_product(product_code)
    if product_code == "costsheet" and not install_password.strip():
        raise HTTPException(status_code=400, detail="Install password is required for Cost Sheet EXE activation")
    prefix = "CS" if product_code == "costsheet" else "AB"
    license_key = (license_key or "").strip() or f"{prefix}-{datetime.now().year}-{secrets.token_hex(2).upper()}"
    customer_id = f"CUST-{secrets.token_hex(3).upper()}"

    with dbmod.SessionLocal() as db:
        if db.scalar(select(Customer).where(Customer.license_key == license_key)):
            raise HTTPException(status_code=400, detail="License key already exists")
        if org_id and not db.get(Organization, org_id):
            raise HTTPException(status_code=400, detail="Organization not found")

        row = Customer(
            customer_id=customer_id,
            organization=organization,
            license_key=license_key,
            status="Active",
            org_id=org_id,
            install_password_hash=hash_password(install_password) if install_password else None,
            require_device_approval=require_device_approval,
            product_code=product_code,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _customer_out(db, row)


def update_customer(customer_id: str, **fields) -> dict:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    with dbmod.SessionLocal() as db:
        row = db.get(Customer, customer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        if fields.get("status") is not None:
            row.status = fields["status"]
        if fields.get("require_device_approval") is not None:
            row.require_device_approval = fields["require_device_approval"]
        if fields.get("org_id") is not None:
            if fields["org_id"] and not db.get(Organization, fields["org_id"]):
                raise HTTPException(status_code=400, detail="Organization not found")
            row.org_id = fields["org_id"] or None
        if fields.get("product_code") is not None:
            row.product_code = _normalize_product(fields["product_code"])
        pwd = fields.get("install_password")
        if pwd is not None:
            row.install_password_hash = hash_password(pwd) if pwd else None
        db.commit()
        db.refresh(row)
        return _customer_out(db, row)


def delete_customer(customer_id: str) -> dict:
    """Delete a customer and its license/device/account records transactionally."""
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")

    with dbmod.SessionLocal() as db:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")

        candidate_org_ids = {customer.org_id} if customer.org_id else set()
        user_rows = db.scalars(
            select(User).where(
                User.customer_id == customer_id,
                User.role != "super_admin",
            )
        ).all()
        user_ids = [user.id for user in user_rows]
        candidate_org_ids.update(user.org_id for user in user_rows if user.org_id)

        installation_ids = list(
            db.scalars(
                select(Installation.installation_id).where(
                    Installation.customer_id == customer_id
                )
            ).all()
        )
        device_count = int(
            db.scalar(
                select(func.count()).select_from(ApprovedDevice).where(
                    ApprovedDevice.customer_id == customer_id
                )
            )
            or 0
        )

        # Keep historical job rows, but unlink references that would otherwise
        # block deletion of their customer, engineer, or activation records.
        db.execute(
            update(Job)
            .where(Job.customer_id == customer_id)
            .values(customer_id=None)
        )
        if user_ids:
            db.execute(
                update(Job)
                .where(Job.user_id.in_(user_ids))
                .values(user_id=None)
            )
        if installation_ids:
            db.execute(
                update(Job)
                .where(Job.installation_id.in_(installation_ids))
                .values(installation_id=None)
            )

        db.execute(
            delete(User).where(
                User.customer_id == customer_id,
                User.role != "super_admin",
            )
        )
        db.execute(delete(ApprovedDevice).where(ApprovedDevice.customer_id == customer_id))
        db.execute(delete(Installation).where(Installation.customer_id == customer_id))
        db.delete(customer)
        db.flush()

        deleted_orgs = 0
        for org_id in candidate_org_ids:
            org = db.get(Organization, org_id)
            if org is None:
                continue
            has_customer = db.scalar(
                select(Customer.customer_id)
                .where(Customer.org_id == org_id)
                .limit(1)
            )
            has_user = db.scalar(
                select(User.id).where(User.org_id == org_id).limit(1)
            )
            if has_customer is None and has_user is None:
                db.delete(org)
                deleted_orgs += 1

        db.commit()
        return {
            "ok": True,
            "deleted_users": len(user_ids),
            "deleted_devices": device_count,
            "deleted_installations": len(installation_ids),
            "deleted_organizations": deleted_orgs,
        }


def list_approved_devices(customer_id: str) -> list[dict]:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    with dbmod.SessionLocal() as db:
        if not db.get(Customer, customer_id):
            raise HTTPException(status_code=404, detail="Customer not found")
        rows = db.scalars(
            select(ApprovedDevice)
            .where(ApprovedDevice.customer_id == customer_id)
            .order_by(ApprovedDevice.created_at.desc())
        ).all()
        return [
            {
                "id": r.id,
                "customer_id": r.customer_id,
                "device_id": r.device_id,
                "hostname": r.hostname,
                "status": r.status,
                "activated_at": r.activated_at.isoformat() if r.activated_at else "",
            }
            for r in rows
        ]


def add_approved_device(customer_id: str, device_id: str, hostname: str) -> dict:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    device_id = device_id.strip().upper()
    hostname = hostname.strip()
    if not device_id or not hostname:
        raise HTTPException(status_code=400, detail="device_id and hostname required")
    if not device_id.startswith("DEV-"):
        raise HTTPException(
            status_code=400,
            detail="device_id must start with 'DEV-' — copy it exactly from the "
            "customer's Activate screen, don't type a raw machine GUID",
        )

    with dbmod.SessionLocal() as db:
        if not db.get(Customer, customer_id):
            raise HTTPException(status_code=404, detail="Customer not found")
        existing = db.scalar(
            select(ApprovedDevice).where(
                ApprovedDevice.customer_id == customer_id,
                ApprovedDevice.device_id == device_id,
            )
        )
        if existing:
            raise HTTPException(status_code=400, detail="Device ID already registered for this customer")

        row = ApprovedDevice(
            id=f"ADEV-{secrets.token_hex(4).upper()}",
            customer_id=customer_id,
            device_id=device_id,
            hostname=hostname,
            status="Pending",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "customer_id": row.customer_id,
            "device_id": row.device_id,
            "hostname": row.hostname,
            "status": row.status,
            "activated_at": "",
        }


def delete_approved_device(customer_id: str, device_id: str) -> dict:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    with dbmod.SessionLocal() as db:
        row = db.scalar(
            select(ApprovedDevice).where(
                ApprovedDevice.customer_id == customer_id,
                ApprovedDevice.id == device_id,
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Approved device not found")
        db.delete(row)
        db.commit()
        return {"ok": True}


def list_installations(customer_id: str | None = None) -> list[dict]:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        raise HTTPException(status_code=503, detail="License server (database) unavailable")
    with dbmod.SessionLocal() as db:
        q = select(Installation).order_by(Installation.created_at.desc())
        if customer_id:
            q = q.where(Installation.customer_id == customer_id)
        rows = db.scalars(q).all()
        return [
            {
                "installation_id": r.installation_id,
                "customer_id": r.customer_id,
                "device_id": r.device_id,
                "hostname": r.hostname or "",
                "status": r.status,
                "activated_at": r.activated_at.isoformat() if r.activated_at else "",
            }
            for r in rows
        ]
