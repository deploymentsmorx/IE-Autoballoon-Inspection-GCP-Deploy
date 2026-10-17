from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_admin
from app.core.config import settings
from app.schemas import AccessUpdate, EngineerCreate, OrgCreate
from app.services import auth_service, customer_service

router = APIRouter(prefix="/api/admin", tags=["users"])


@router.get("/organizations")
def list_orgs(_: dict = Depends(require_admin)):
    orgs = auth_service.list_orgs()
    if settings.costsheet_admin_portal_only:
        customer_org_ids = {
            customer["org_id"]
            for customer in customer_service.list_customers()
            if customer.get("product_code") == "costsheet" and customer.get("org_id")
        }
        return [org for org in orgs if org["id"] in customer_org_ids]
    return orgs


@router.post("/organizations")
def create_org(body: OrgCreate, _: dict = Depends(require_admin)):
    return auth_service.create_org(body.name)


@router.get("/engineers")
def list_engineers(_: dict = Depends(require_admin)):
    engineers = auth_service.list_engineers()
    if settings.costsheet_admin_portal_only:
        costsheet_customer_ids = {
            customer["customer_id"]
            for customer in customer_service.list_customers()
            if customer.get("product_code") == "costsheet"
        }
        return [engineer for engineer in engineers if engineer.get("customer_id") in costsheet_customer_ids]
    return engineers


@router.post("/engineers")
def add_engineer(body: EngineerCreate, _: dict = Depends(require_admin)):
    return auth_service.add_engineer(
        body.full_name,
        body.email,
        body.org_id,
        body.password,
        body.customer_id,
    )


@router.delete("/engineers/{user_id}")
def delete_engineer(user_id: str, _: dict = Depends(require_admin)):
    return auth_service.delete_engineer(user_id)


@router.put("/access")
def update_access(body: AccessUpdate, _: dict = Depends(require_admin)):
    return auth_service.update_access(body.user_id, body.active, body.read, body.write, body.delete)


@router.post("/access/grant-full/{user_id}")
def grant_full(user_id: str, _: dict = Depends(require_admin)):
    return auth_service.grant_full(user_id)
