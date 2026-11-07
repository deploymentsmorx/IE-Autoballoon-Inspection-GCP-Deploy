"""Local JSON fallback when Postgres is unreachable."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[2]  # backend/
DB_PATH = ROOT / "data" / "smorx.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _with_collections(db: dict) -> dict:
    db.setdefault("users", [])
    db.setdefault("organizations", [])
    db.setdefault("customers", [])
    db.setdefault("approved_devices", [])
    db.setdefault("installations", [])
    for user in db["users"]:
        user.setdefault("customer_id", None)
        user.setdefault("permissions", {"read": False, "write": False, "delete": False})
    return db


def ensure_seed(hash_password) -> dict:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        return _with_collections(json.loads(DB_PATH.read_text(encoding="utf-8")))
    db = _with_collections({
        "users": [
            {
                "id": "u-admin",
                "username": settings.superadmin_username,
                "email": settings.superadmin_email,
                "full_name": settings.superadmin_name,
                "password_hash": hash_password(settings.superadmin_password),
                "role": "super_admin",
                "org_id": None,
                "active": True,
                "permissions": {"read": True, "write": True, "delete": True},
            }
        ],
        "updated_at": _now(),
    })
    save(db)
    return db


def load() -> dict:
    if not DB_PATH.exists():
        from app.core.security import hash_password

        return ensure_seed(hash_password)
    try:
        return _with_collections(json.loads(DB_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        from app.core.security import hash_password

        return ensure_seed(hash_password)


def save(db: dict) -> None:
    db["updated_at"] = _now()
    DB_PATH.write_text(json.dumps(_with_collections(db), indent=2), encoding="utf-8")
