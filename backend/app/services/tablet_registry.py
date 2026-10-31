"""Factory tablets that check in to this Auto Balloon PC."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "tablets.json"
_LOCK = threading.Lock()
_DEVICE_RE = re.compile(r"^DEV-[A-Z0-9-]{4,80}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    if not DB_PATH.exists():
        return {"tablets": []}
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tablets": []}
    if not isinstance(data, dict) or not isinstance(data.get("tablets"), list):
        return {"tablets": []}
    return data


def _save(data: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize_device_id(device_id: str) -> str:
    device_id = (device_id or "").strip().upper()
    if not _DEVICE_RE.match(device_id):
        raise HTTPException(
            status_code=400,
            detail="device_id must look like DEV-… — copy it from the tablet setup screen",
        )
    return device_id


def check_in(device_id: str, hostname: str, label: str = "") -> dict:
    device_id = normalize_device_id(device_id)
    hostname = (hostname or "").strip()
    label = (label or hostname or "Tablet").strip()
    if not hostname:
        raise HTTPException(status_code=400, detail="Tablet name is required")
    if len(hostname) > 80 or len(label) > 80:
        raise HTTPException(status_code=400, detail="Tablet name is too long")
    with _LOCK:
        data = _load()
        rows: list[dict] = data["tablets"]
        row = next((r for r in rows if r.get("device_id") == device_id), None)
        now = _now()
        if row is None:
            row = {
                "device_id": device_id,
                "hostname": hostname,
                "label": label,
                "status": "pending",
                "first_seen": now,
                "last_seen": now,
                "approved_at": "",
            }
            rows.append(row)
        else:
            row["hostname"] = hostname
            row["label"] = label or row.get("label") or hostname
            row["last_seen"] = now
            if row.get("status") not in {"pending", "approved", "rejected"}:
                row["status"] = "pending"
        _save(data)
        return dict(row)


def get_status(device_id: str) -> dict:
    device_id = normalize_device_id(device_id)
    with _LOCK:
        data = _load()
        row = next((r for r in data["tablets"] if r.get("device_id") == device_id), None)
    if row is None:
        return {"device_id": device_id, "status": "unknown", "hostname": "", "label": ""}
    return dict(row)


def list_tablets() -> list[dict]:
    with _LOCK:
        data = _load()
        rows = [dict(r) for r in data["tablets"]]
    rows.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return rows


def set_status(device_id: str, status: str) -> dict:
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(status_code=400, detail="Unknown tablet status")
    device_id = normalize_device_id(device_id)
    with _LOCK:
        data = _load()
        row = next((r for r in data["tablets"] if r.get("device_id") == device_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="Tablet has not checked in yet")
        row["status"] = status
        row["approved_at"] = _now() if status == "approved" else ""
        _save(data)
        return dict(row)


def remove(device_id: str) -> dict:
    device_id = normalize_device_id(device_id)
    with _LOCK:
        data = _load()
        before = len(data["tablets"])
        data["tablets"] = [r for r in data["tablets"] if r.get("device_id") != device_id]
        if len(data["tablets"]) == before:
            raise HTTPException(status_code=404, detail="Tablet not found")
        _save(data)
    return {"ok": True}


def require_approved(device_id: str) -> dict:
    row = get_status(device_id)
    status = row.get("status")
    if status == "approved":
        return row
    if status == "pending":
        raise HTTPException(
            status_code=403,
            detail="This tablet is waiting for Super Admin approval",
        )
    if status == "rejected":
        raise HTTPException(status_code=403, detail="This tablet was rejected by Super Admin")
    raise HTTPException(
        status_code=403,
        detail="This tablet is not registered. Open tablet setup and check in first.",
    )
