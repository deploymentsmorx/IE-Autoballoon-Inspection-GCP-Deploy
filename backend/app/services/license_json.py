"""JSON fallback for customer licenses, approved devices, and EXE activations."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.config import settings
from app.core.security import create_token, hash_password, verify_password
from app.services import local_store

VALID_PRODUCTS = frozenset({"autoballoon", "costsheet"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_product(product_code: str | None) -> str:
    code = (product_code or "autoballoon").strip().lower()
    if code not in VALID_PRODUCTS:
        raise HTTPException(
            status_code=400,
            detail=f"product_code must be one of: {', '.join(sorted(VALID_PRODUCTS))}",
        )
    return code


def _org_tenant(db: dict, org_id: str | None) -> str | None:
    if not org_id:
        return None
    org = next((o for o in db["organizations"] if o.get("id") == org_id), None)
    return org.get("tenant_id") if org else None


def _customer_out(db: dict, c: dict) -> dict:
    cid = c["customer_id"]
    approved = sum(1 for d in db["approved_devices"] if d.get("customer_id") == cid)
    installs = sum(
        1
        for i in db["installations"]
        if i.get("customer_id") == cid and i.get("status") == "Active"
    )
    return {
        "customer_id": cid,
        "organization": c.get("organization") or "",
        "license_key": c.get("license_key") or "",
        "status": c.get("status") or "Active",
        "org_id": c.get("org_id"),
        "tenant_id": _org_tenant(db, c.get("org_id")),
        "product_code": c.get("product_code") or "autoballoon",
        "require_device_approval": bool(c.get("require_device_approval", True)),
        "has_install_password": bool(c.get("install_password_hash")),
        "approved_devices": approved,
        "active_installations": installs,
    }


def list_customers() -> list[dict]:
    db = local_store.load()
    rows = [_customer_out(db, c) for c in db["customers"]]
    rows.sort(key=lambda r: (r.get("organization") or "").lower())
    return rows


def create_customer(
    *,
    organization: str,
    license_key: str = "",
    org_id: str | None = None,
    install_password: str = "",
    require_device_approval: bool = True,
    product_code: str = "autoballoon",
) -> dict:
    organization = organization.strip()
    if not organization:
        raise HTTPException(status_code=400, detail="organization required")
    product_code = _normalize_product(product_code)
    if product_code == "costsheet" and not install_password.strip():
        raise HTTPException(status_code=400, detail="Install password is required for Cost Sheet EXE activation")
    prefix = "CS" if product_code == "costsheet" else "AB"
    license_key = (license_key or "").strip() or f"{prefix}-{datetime.now().year}-{secrets.token_hex(2).upper()}"
    customer_id = f"CUST-{secrets.token_hex(3).upper()}"
    db = local_store.load()
    if any(c.get("license_key") == license_key for c in db["customers"]):
        raise HTTPException(status_code=400, detail="License key already exists")
    if org_id and not any(o.get("id") == org_id for o in db["organizations"]):
        raise HTTPException(status_code=400, detail="Organization not found")
    row = {
        "customer_id": customer_id,
        "organization": organization,
        "license_key": license_key,
        "status": "Active",
        "org_id": org_id,
        "install_password_hash": hash_password(install_password) if install_password else None,
        "require_device_approval": require_device_approval,
        "product_code": product_code,
        "created_at": _now(),
    }
    db["customers"].append(row)
    local_store.save(db)
    return _customer_out(db, row)


def update_customer(customer_id: str, **fields) -> dict:
    db = local_store.load()
    row = next((c for c in db["customers"] if c.get("customer_id") == customer_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if fields.get("status") is not None:
        row["status"] = fields["status"]
    if fields.get("require_device_approval") is not None:
        row["require_device_approval"] = fields["require_device_approval"]
    if fields.get("org_id") is not None:
        org_id = fields["org_id"] or None
        if org_id and not any(o.get("id") == org_id for o in db["organizations"]):
            raise HTTPException(status_code=400, detail="Organization not found")
        row["org_id"] = org_id
    if fields.get("product_code") is not None:
        row["product_code"] = _normalize_product(fields["product_code"])
    pwd = fields.get("install_password")
    if pwd is not None:
        row["install_password_hash"] = hash_password(pwd) if pwd else None
    local_store.save(db)
    return _customer_out(db, row)


def delete_customer(customer_id: str) -> dict:
    db = local_store.load()
    customer = next((c for c in db["customers"] if c.get("customer_id") == customer_id), None)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    user_rows = [
        u for u in db["users"] if u.get("customer_id") == customer_id and u.get("role") != "super_admin"
    ]
    user_ids = {u["id"] for u in user_rows}
    candidate_org_ids = {customer.get("org_id")} if customer.get("org_id") else set()
    candidate_org_ids.update(u.get("org_id") for u in user_rows if u.get("org_id"))
    installation_ids = [
        i.get("installation_id") for i in db["installations"] if i.get("customer_id") == customer_id
    ]
    device_count = sum(1 for d in db["approved_devices"] if d.get("customer_id") == customer_id)
    db["users"] = [u for u in db["users"] if u.get("id") not in user_ids]
    db["approved_devices"] = [d for d in db["approved_devices"] if d.get("customer_id") != customer_id]
    db["installations"] = [i for i in db["installations"] if i.get("customer_id") != customer_id]
    db["customers"] = [c for c in db["customers"] if c.get("customer_id") != customer_id]
    deleted_orgs = 0
    remaining_orgs = []
    for org in db["organizations"]:
        org_id = org.get("id")
        if org_id not in candidate_org_ids:
            remaining_orgs.append(org)
            continue
        has_customer = any(c.get("org_id") == org_id for c in db["customers"])
        has_user = any(u.get("org_id") == org_id for u in db["users"])
        if has_customer or has_user:
            remaining_orgs.append(org)
        else:
            deleted_orgs += 1
    db["organizations"] = remaining_orgs
    local_store.save(db)
    return {
        "ok": True,
        "deleted_users": len(user_ids),
        "deleted_devices": device_count,
        "deleted_installations": len(installation_ids),
        "deleted_organizations": deleted_orgs,
    }


def list_approved_devices(customer_id: str) -> list[dict]:
    db = local_store.load()
    if not any(c.get("customer_id") == customer_id for c in db["customers"]):
        raise HTTPException(status_code=404, detail="Customer not found")
    rows = [d for d in db["approved_devices"] if d.get("customer_id") == customer_id]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [
        {
            "id": r.get("id"),
            "customer_id": r.get("customer_id"),
            "device_id": r.get("device_id"),
            "hostname": r.get("hostname"),
            "status": r.get("status") or "Pending",
            "activated_at": r.get("activated_at") or "",
        }
        for r in rows
    ]


def add_approved_device(customer_id: str, device_id: str, hostname: str) -> dict:
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
    db = local_store.load()
    if not any(c.get("customer_id") == customer_id for c in db["customers"]):
        raise HTTPException(status_code=404, detail="Customer not found")
    if any(
        d.get("customer_id") == customer_id and d.get("device_id") == device_id
        for d in db["approved_devices"]
    ):
        raise HTTPException(status_code=400, detail="Device ID already registered for this customer")
    row = {
        "id": f"ADEV-{secrets.token_hex(4).upper()}",
        "customer_id": customer_id,
        "device_id": device_id,
        "hostname": hostname,
        "status": "Pending",
        "created_at": _now(),
        "activated_at": "",
    }
    db["approved_devices"].append(row)
    local_store.save(db)
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "device_id": row["device_id"],
        "hostname": row["hostname"],
        "status": row["status"],
        "activated_at": "",
    }


def delete_approved_device(customer_id: str, device_id: str) -> dict:
    db = local_store.load()
    before = len(db["approved_devices"])
    db["approved_devices"] = [
        d
        for d in db["approved_devices"]
        if not (d.get("customer_id") == customer_id and d.get("id") == device_id)
    ]
    if len(db["approved_devices"]) == before:
        raise HTTPException(status_code=404, detail="Approved device not found")
    local_store.save(db)
    return {"ok": True}


def list_installations(customer_id: str | None = None) -> list[dict]:
    db = local_store.load()
    rows = db["installations"]
    if customer_id:
        rows = [r for r in rows if r.get("customer_id") == customer_id]
    rows = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)
    return [
        {
            "installation_id": r.get("installation_id"),
            "customer_id": r.get("customer_id"),
            "device_id": r.get("device_id"),
            "hostname": r.get("hostname") or "",
            "status": r.get("status"),
            "activated_at": r.get("activated_at") or "",
        }
        for r in rows
    ]


def device_status(device_id: str, product_code: str = "autoballoon") -> dict:
    device_id = device_id.strip().upper()
    if not device_id:
        return {"activated": False}
    try:
        product_code = _normalize_product(product_code)
    except HTTPException:
        return {"activated": False}
    db = local_store.load()
    inst = next(
        (
            i
            for i in db["installations"]
            if i.get("device_id") == device_id and i.get("status") == "Active"
        ),
        None,
    )
    if inst is None:
        return {"activated": False}
    customer = next((c for c in db["customers"] if c.get("customer_id") == inst.get("customer_id")), None)
    if customer is None or customer.get("status") != "Active":
        return {"activated": False}
    cust_product = customer.get("product_code") or "autoballoon"
    if cust_product != product_code:
        return {"activated": False}
    if customer.get("require_device_approval", True):
        approved = next(
            (
                d
                for d in db["approved_devices"]
                if d.get("customer_id") == customer["customer_id"] and d.get("device_id") == device_id
            ),
            None,
        )
        if approved is None or approved.get("status") != "Active":
            return {"activated": False}
        if (inst.get("hostname") or "").strip().lower() != (approved.get("hostname") or "").strip().lower():
            return {"activated": False}
    return {
        "activated": True,
        "license_key": customer.get("license_key"),
        "customer_id": customer.get("customer_id"),
        "installation_id": inst.get("installation_id"),
        "product_code": cust_product,
    }


def verify_device_for_login(customer_id: str, device_id: str, hostname: str) -> None:
    device_id = device_id.strip().upper()
    hostname = hostname.strip()
    db = local_store.load()
    inst = next(
        (
            i
            for i in db["installations"]
            if i.get("customer_id") == customer_id and i.get("status") == "Active"
        ),
        None,
    )
    if inst is None:
        raise HTTPException(
            status_code=403,
            detail="No active installation registered for this account — activate on this device first",
        )
    if inst.get("device_id") != device_id or (inst.get("hostname") or "").strip().lower() != hostname.lower():
        raise HTTPException(
            status_code=403,
            detail="This device does not match the registered installation for this account",
        )


def verify_user_product(user: dict, product_code: str, required_permission: str = "read") -> dict:
    product_code = _normalize_product(product_code)
    if user.get("role") == "super_admin":
        admin_product = "costsheet" if settings.costsheet_admin_portal_only else "autoballoon"
        if product_code != admin_product:
            raise HTTPException(status_code=403, detail="Super admin access is not enabled for this product")
        return {
            "customer_id": None,
            "organization": "Cost Sheet" if admin_product == "costsheet" else "Inspection",
            "product_code": product_code,
            "super_admin": True,
        }
    if not user.get("active") or not (user.get("permissions") or {}).get(required_permission):
        raise HTTPException(status_code=403, detail="Account access is not active")
    customer_id = user.get("customer_id")
    if not customer_id:
        raise HTTPException(status_code=403, detail="Account is not linked to a customer license")
    db = local_store.load()
    customer = next((c for c in db["customers"] if c.get("customer_id") == customer_id), None)
    if customer is None or customer.get("status") != "Active":
        raise HTTPException(status_code=403, detail="Customer license is inactive")
    customer_product = customer.get("product_code") or "autoballoon"
    if customer_product != product_code:
        raise HTTPException(status_code=403, detail="Account is not provisioned for Cost Sheet")
    return {
        "customer_id": customer["customer_id"],
        "organization": customer.get("organization"),
        "product_code": customer_product,
    }


def activate_installation(
    *,
    license_key: str,
    username: str,
    password: str,
    device_id: str,
    hostname: str = "",
    install_password: str = "",
    product_code: str = "autoballoon",
) -> dict:
    from app.services import auth_service

    product_code = _normalize_product(product_code)
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

    db = local_store.load()
    customer = next((c for c in db["customers"] if c.get("license_key") == license_key), None)
    if customer is None or customer.get("status") != "Active":
        raise HTTPException(status_code=403, detail="Invalid or inactive license")
    cust_product = customer.get("product_code") or "autoballoon"
    if cust_product != product_code:
        raise HTTPException(
            status_code=403,
            detail=f"This license is for '{cust_product}', not '{product_code}'",
        )
    if product_code == "costsheet":
        if user.get("role") == "super_admin":
            raise HTTPException(
                status_code=403,
                detail="Super Admin accounts sign in through the Cost Sheet web app, not a customer EXE license.",
            )
        if user.get("customer_id") != customer["customer_id"]:
            raise HTTPException(
                status_code=403,
                detail="This sign-in account is not linked to the license provided.",
            )
    if product_code == "costsheet" and not customer.get("install_password_hash"):
        raise HTTPException(
            status_code=403,
            detail="No install password is configured for this license. Contact the administrator.",
        )
    if customer.get("install_password_hash"):
        if not install_password or not verify_password(install_password, customer["install_password_hash"]):
            raise HTTPException(status_code=403, detail="Invalid install password")
    if customer.get("require_device_approval", True):
        approved = next(
            (
                d
                for d in db["approved_devices"]
                if d.get("customer_id") == customer["customer_id"] and d.get("device_id") == device_id
            ),
            None,
        )
        if approved is None:
            raise HTTPException(status_code=403, detail="Device not approved — contact SmorX admin")
        if (approved.get("hostname") or "").strip().lower() != hostname.lower():
            raise HTTPException(status_code=403, detail="Hostname does not match admin record")
        if approved.get("status") == "Revoked":
            raise HTTPException(status_code=403, detail="Device approval revoked")
    other = next(
        (
            i
            for i in db["installations"]
            if i.get("customer_id") == customer["customer_id"]
            and i.get("status") == "Active"
            and i.get("device_id") != device_id
        ),
        None,
    )
    if other is not None:
        raise HTTPException(status_code=403, detail="License already registered to another device")
    now = _now()
    inst = next(
        (
            i
            for i in db["installations"]
            if i.get("customer_id") == customer["customer_id"] and i.get("device_id") == device_id
        ),
        None,
    )
    if inst is None:
        inst = {
            "installation_id": f"INST-{secrets.token_hex(4).upper()}",
            "customer_id": customer["customer_id"],
            "device_id": device_id,
            "hostname": hostname,
            "status": "Active",
            "activated_at": now,
            "last_seen_at": now,
            "created_at": now,
        }
        db["installations"].append(inst)
    else:
        if inst.get("status") != "Active":
            raise HTTPException(status_code=403, detail="Installation blocked")
        inst["hostname"] = hostname
        inst["last_seen_at"] = now
        inst["activated_at"] = inst.get("activated_at") or now
    if customer.get("require_device_approval", True):
        approved = next(
            (
                d
                for d in db["approved_devices"]
                if d.get("customer_id") == customer["customer_id"] and d.get("device_id") == device_id
            ),
            None,
        )
        if approved is not None:
            approved["status"] = "Active"
            approved["activated_at"] = now
    db_user = next((u for u in db["users"] if u.get("id") == user["id"]), None)
    if db_user is not None and not db_user.get("customer_id"):
        db_user["customer_id"] = customer["customer_id"]
    local_store.save(db)
    token = create_token(user)
    return {
        "token": token,
        "customer": {
            "customer_id": customer["customer_id"],
            "organization": customer.get("organization"),
            "license_key": customer.get("license_key"),
            "status": customer.get("status"),
            "product_code": cust_product,
        },
        "installation": {
            "installation_id": inst["installation_id"],
            "device_id": inst["device_id"],
            "hostname": inst.get("hostname") or "",
            "status": inst.get("status"),
            "activated_at": inst.get("activated_at") or "",
        },
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "must_change_password": bool(user.get("must_change_password")),
        },
    }
