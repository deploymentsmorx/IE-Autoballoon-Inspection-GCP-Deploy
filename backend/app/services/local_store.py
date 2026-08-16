"""Local JSON fallback when Neon is unreachable."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # backend/
DB_PATH = ROOT / "data" / "smorx.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_seed(hash_password) -> dict:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    db = {
        "users": [
            {
                "id": "u-admin",
                "username": "admin",
                "email": "admin@smorx.ai",
                "full_name": "Super Admin",
                "password_hash": hash_password("admin123"),
                "role": "super_admin",
                "org_id": None,
                "active": True,
                "permissions": {"read": True, "write": True, "delete": True},
            }
        ],
        "organizations": [],
        "updated_at": _now(),
    }
    save(db)
    return db


def load() -> dict:
    return json.loads(DB_PATH.read_text(encoding="utf-8"))


def save(db: dict) -> None:
    db["updated_at"] = _now()
    DB_PATH.write_text(json.dumps(db, indent=2), encoding="utf-8")
