from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_admin
from app.schemas import (
    ApprovedDeviceCreate,
    CustomerCreate,
    CustomerUpdate,
)
from app.services import customer_service

router = APIRouter(prefix="/api/admin", tags=["customers"])


@router.get("/customers")
def list_customers(_: dict = Depends(require_admin)):
    return customer_service.list_customers()


@router.post("/customers")
def create_customer(body: CustomerCreate, _: dict = Depends(require_admin)):
    return customer_service.create_customer(
        organization=body.organization,
        license_key=body.license_key,
        org_id=body.org_id,
        install_password=body.install_password,
        require_device_approval=body.require_device_approval,
        product_code=body.product_code,
    )


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: str, body: CustomerUpdate, _: dict = Depends(require_admin)):
    return customer_service.update_customer(
        customer_id,
        status=body.status,
        install_password=body.install_password,
        require_device_approval=body.require_device_approval,
        org_id=body.org_id,
        product_code=body.product_code,
    )


@router.get("/customers/{customer_id}/devices")
def list_devices(customer_id: str, _: dict = Depends(require_admin)):
    return customer_service.list_approved_devices(customer_id)


@router.post("/customers/{customer_id}/devices")
def add_device(customer_id: str, body: ApprovedDeviceCreate, _: dict = Depends(require_admin)):
    return customer_service.add_approved_device(customer_id, body.device_id, body.hostname)


@router.delete("/customers/{customer_id}/devices/{device_row_id}")
def remove_device(customer_id: str, device_row_id: str, _: dict = Depends(require_admin)):
    return customer_service.delete_approved_device(customer_id, device_row_id)


@router.get("/installations")
def list_installations(customer_id: str | None = None, _: dict = Depends(require_admin)):
    return customer_service.list_installations(customer_id)
