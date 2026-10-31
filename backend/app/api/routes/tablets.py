"""Tablet check-in for factory inspection clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import require_admin
from app.services import tablet_registry

router = APIRouter(tags=["tablets"])


class TabletCheckIn(BaseModel):
    device_id: str
    hostname: str
    label: str = ""


@router.post("/api/tablets/check-in")
def check_in(body: TabletCheckIn):
    return tablet_registry.check_in(body.device_id, body.hostname, body.label)


@router.get("/api/tablets/status")
def status(device_id: str):
    return tablet_registry.get_status(device_id)


@router.get("/api/admin/tablets")
def list_tablets(user: dict = Depends(require_admin)):
    return tablet_registry.list_tablets()


@router.post("/api/admin/tablets/{device_id}/approve")
def approve(device_id: str, user: dict = Depends(require_admin)):
    return tablet_registry.set_status(device_id, "approved")


@router.post("/api/admin/tablets/{device_id}/reject")
def reject(device_id: str, user: dict = Depends(require_admin)):
    return tablet_registry.set_status(device_id, "rejected")


@router.delete("/api/admin/tablets/{device_id}")
def remove(device_id: str, user: dict = Depends(require_admin)):
    return tablet_registry.remove(device_id)
