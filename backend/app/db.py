"""Compatibility shim — paths + schemas + security."""

from __future__ import annotations

from app.core.config import DATA, JOBS, ROOT, STATIC, UPLOADS
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.schemas import (
    AccessUpdate,
    Balloon,
    BalloonUpdate,
    EngineerCreate,
    LoginRequest,
    MeasureCol,
    OrgCreate,
    ReportPayload,
    ReportRow,
)

__all__ = [
    "ROOT",
    "DATA",
    "UPLOADS",
    "JOBS",
    "STATIC",
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "LoginRequest",
    "OrgCreate",
    "EngineerCreate",
    "AccessUpdate",
    "Balloon",
    "BalloonUpdate",
    "MeasureCol",
    "ReportRow",
    "ReportPayload",
]
