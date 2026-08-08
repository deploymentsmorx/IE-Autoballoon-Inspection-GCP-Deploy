"""Unified user/org store: Neon when available, else local JSON."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from . import local_store
from .db import hash_password, verify_password
from .postgres import (
    Organization,
    SessionLocal,
    USE_POSTGRES,
    User,
    init_db,
    org_to_dict,
    user_to_dict,
)


def startup(hash_pw=hash_password) -> str:
    return init_db(hash_pw)


def find_user_by_login(key: str) -> dict | None:
    key = key.strip().lower()
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            users = db.scalars(select(User)).all()
            user = next(
                (u for u in users if u.username.lower() == key or u.email.lower() == key),
                None,
            )
            return user_to_dict(user) if user else None
    db = local_store.load()
    return next(
        (
            u
            for u in db["users"]
            if u["username"].lower() == key or u["email"].lower() == key
        ),
        None,
    )


def get_user(user_id: str) -> dict | None:
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            return user_to_dict(user) if user else None
    db = local_store.load()
    return next((u for u in db["users"] if u["id"] == user_id), None)


def list_orgs() -> list[dict]:
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            orgs = db.scalars(select(Organization).order_by(Organization.name)).all()
            out = []
            for o in orgs:
                n = db.scalar(
                    select(func.count()).select_from(User).where(User.org_id == o.id)
                )
                out.append(org_to_dict(o, int(n or 0)))
            return out
    db = local_store.load()
    out = []
    for o in db["organizations"]:
        eng = sum(1 for u in db["users"] if u.get("org_id") == o["id"])
        out.append({**o, "engineers": eng})
    return out


def create_org(name: str) -> dict:
    oid = f"org-{secrets.token_hex(4)}"
    trial = (datetime.now(timezone.utc) + timedelta(days=30)).date()
    if USE_POSTGRES and SessionLocal is not None:
        org = Organization(
            id=oid,
            name=name.strip(),
            tenant_id=oid.upper(),
            status="Active",
            trial_ends=trial,
        )
        with SessionLocal() as db:
            db.add(org)
            db.commit()
            return org_to_dict(org, 0)
    db = local_store.load()
    org = {
        "id": oid,
        "name": name.strip(),
        "tenant_id": oid.upper(),
        "status": "Active",
        "trial_ends": trial.isoformat(),
    }
    db["organizations"].append(org)
    local_store.save(db)
    return {**org, "engineers": 0}


def list_engineers() -> list[dict]:
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            users = db.scalars(select(User).where(User.role != "super_admin")).all()
            return [
                {
                    "id": u.id,
                    "full_name": u.full_name,
                    "email": u.email,
                    "username": u.username,
                    "status": "Active" if u.active else "Inactive",
                    "org_id": u.org_id,
                    "permissions": {
                        "read": u.perm_read,
                        "write": u.perm_write,
                        "delete": u.perm_delete,
                    },
                    "active": u.active,
                    "role": u.role,
                }
                for u in users
            ]
    db = local_store.load()
    return [
        {
            "id": u["id"],
            "full_name": u["full_name"],
            "email": u["email"],
            "username": u["username"],
            "status": "Active" if u.get("active") else "Inactive",
            "org_id": u.get("org_id"),
            "permissions": u.get("permissions") or {},
            "active": u.get("active", False),
            "role": u.get("role"),
        }
        for u in db["users"]
        if u.get("role") != "super_admin"
    ]


def add_engineer(full_name: str, email: str, org_id: str, password: str) -> dict:
    email = email.strip().lower()
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            if not db.get(Organization, org_id):
                raise HTTPException(status_code=400, detail="Organization not found")
            if db.scalar(select(User).where(User.email == email)):
                raise HTTPException(status_code=400, detail="Email already exists")
            username = email.split("@")[0]
            base = username
            n = 1
            while db.scalar(select(User).where(User.username == username)):
                username = f"{base}{n}"
                n += 1
            user = User(
                id=f"u-{secrets.token_hex(4)}",
                username=username,
                email=email,
                full_name=full_name.strip(),
                password_hash=hash_password(password),
                role="engineer",
                org_id=org_id,
                active=False,
                perm_read=False,
                perm_write=False,
                perm_delete=False,
            )
            db.add(user)
            db.commit()
            return {
                "id": user.id,
                "username": username,
                "email": email,
                "temp_password": password,
            }

    db = local_store.load()
    if not any(o["id"] == org_id for o in db["organizations"]):
        raise HTTPException(status_code=400, detail="Organization not found")
    if any(u["email"].lower() == email for u in db["users"]):
        raise HTTPException(status_code=400, detail="Email already exists")
    username = email.split("@")[0]
    base = username
    n = 1
    while any(u["username"].lower() == username.lower() for u in db["users"]):
        username = f"{base}{n}"
        n += 1
    user = {
        "id": f"u-{secrets.token_hex(4)}",
        "username": username,
        "email": email,
        "full_name": full_name.strip(),
        "password_hash": hash_password(password),
        "role": "engineer",
        "org_id": org_id,
        "active": False,
        "permissions": {"read": False, "write": False, "delete": False},
    }
    db["users"].append(user)
    local_store.save(db)
    return {
        "id": user["id"],
        "username": username,
        "email": email,
        "temp_password": password,
    }


def update_access(user_id: str, active: bool, read: bool, write: bool, delete: bool) -> dict:
    if USE_POSTGRES and SessionLocal is not None:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            if user.role == "super_admin":
                raise HTTPException(status_code=400, detail="Cannot edit super admin here")
            user.active = active
            user.perm_read = read
            user.perm_write = write
            user.perm_delete = delete
            db.commit()
            return {"ok": True, "user_id": user.id}
    db = local_store.load()
    user = next((u for u in db["users"] if u["id"] == user_id), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.get("role") == "super_admin":
        raise HTTPException(status_code=400, detail="Cannot edit super admin here")
    user["active"] = active
    user["permissions"] = {"read": read, "write": write, "delete": delete}
    local_store.save(db)
    return {"ok": True, "user_id": user_id}


def grant_full(user_id: str) -> dict:
    return update_access(user_id, True, True, True, True)
