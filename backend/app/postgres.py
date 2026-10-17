"""Postgres facade — models live in app.models, engine in app.core.database."""

from __future__ import annotations

import json

from app.core import database as dbmod
from app.core.database import Base, init_db
from app.models import (
    Customer,
    Installation,
    Job,
    Organization,
    User,
)


def __getattr__(name: str):
    """Resolve SessionLocal / USE_POSTGRES / engine after startup rebinds them."""
    if name in {"SessionLocal", "USE_POSTGRES", "engine"}:
        return getattr(dbmod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "full_name": u.full_name,
        "password_hash": u.password_hash,
        "role": u.role,
        "org_id": u.org_id,
        "customer_id": getattr(u, "customer_id", None),
        "active": bool(u.active),
        "permissions": {
            "read": bool(u.perm_read),
            "write": bool(u.perm_write),
            "delete": bool(u.perm_delete),
        },
        "must_change_password": bool(getattr(u, "must_change_password", False)),
    }


def org_to_dict(o: Organization, engineers: int = 0) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "tenant_id": o.tenant_id,
        "engineers": engineers,
        "status": o.status,
        "trial_ends": o.trial_ends.isoformat() if o.trial_ends else "",
    }


def job_to_dict(j: Job) -> dict:
    snap: dict = {}
    if j.meta_json:
        try:
            snap = json.loads(j.meta_json)
        except Exception:
            snap = {}
    return {
        "id": j.job_id,
        "customer_id": j.customer_id,
        "user_id": j.user_id,
        "installation_id": j.installation_id,
        "status": j.status,
        "part_number": j.part_number or "",
        "part_name": j.part_name or "",
        "revision": j.revision or "",
        "page_count": j.page_count or 0,
        "s3_input_key": j.s3_input_key,
        "s3_output_key": j.s3_output_key,
        "files": snap.get("files") or [],
        "balloons": snap.get("balloons") or [],
        "created_at": j.created_at.isoformat() if j.created_at else "",
        "updated_at": j.updated_at.isoformat() if j.updated_at else "",
    }


def installation_to_dict(i: Installation) -> dict:
    return {
        "installation_id": i.installation_id,
        "customer_id": i.customer_id,
        "device_id": i.device_id,
        "status": i.status,
        "activated_at": i.activated_at.isoformat() if i.activated_at else "",
        "last_seen_at": i.last_seen_at.isoformat() if i.last_seen_at else "",
    }


__all__ = [
    "Base",
    "Customer",
    "Installation",
    "Job",
    "Organization",
    "User",
    "init_db",
    "user_to_dict",
    "org_to_dict",
    "job_to_dict",
    "installation_to_dict",
]
