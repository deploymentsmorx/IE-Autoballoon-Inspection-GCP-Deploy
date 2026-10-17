from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str
    # Cost Sheet browser login is scoped to the customer's active Cost Sheet license.
    product_code: str | None = None
    # Sent by the desktop EXE only — web logins omit these and skip the check.
    device_id: str | None = None
    hostname: str | None = None


class OrgCreate(BaseModel):
    name: str


class EngineerCreate(BaseModel):
    full_name: str
    email: str
    org_id: str
    customer_id: str | None = None
    password: str = "engineer123"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class AccessUpdate(BaseModel):
    user_id: str
    active: bool = False
    read: bool = False
    write: bool = False
    delete: bool = False


class LicenseActivateRequest(BaseModel):
    license_key: str
    username: str
    password: str
    device_id: str
    hostname: str = ""
    install_password: str = ""
    # Which desktop product is activating (autoballoon | costsheet)
    product_code: str = "autoballoon"
