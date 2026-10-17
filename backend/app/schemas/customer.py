from __future__ import annotations

from pydantic import BaseModel, Field


class CustomerOut(BaseModel):
    customer_id: str
    organization: str
    license_key: str
    status: str
    org_id: str | None = None
    tenant_id: str | None = None
    product_code: str = "autoballoon"
    require_device_approval: bool = True
    has_install_password: bool = False
    approved_devices: int = 0
    active_installations: int = 0


class CustomerCreate(BaseModel):
    organization: str
    license_key: str = ""
    org_id: str | None = None
    install_password: str = ""
    require_device_approval: bool = True
    product_code: str = "autoballoon"


class CustomerUpdate(BaseModel):
    status: str | None = None
    install_password: str | None = None
    require_device_approval: bool | None = None
    org_id: str | None = None
    product_code: str | None = None


class ApprovedDeviceOut(BaseModel):
    id: str
    customer_id: str
    device_id: str
    hostname: str
    status: str
    activated_at: str = ""


class ApprovedDeviceCreate(BaseModel):
    device_id: str
    hostname: str


class InstallationOut(BaseModel):
    installation_id: str
    customer_id: str
    device_id: str
    hostname: str = ""
    status: str
    activated_at: str = ""
