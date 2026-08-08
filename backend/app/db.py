from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / "static"

SECRET = os.getenv("JWT_SECRET", "smorx-dev-secret-change-in-prod")
ALGORITHM = "HS256"
_PWD_PEPPER = os.getenv("PWD_PEPPER", "smorx-pepper")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(plain: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(f"{_PWD_PEPPER}:{salt}:{plain}".encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(plain: str, hashed: str) -> bool:
    try:
        kind, salt, digest = hashed.split("$", 2)
        if kind != "sha256":
            return False
        check = hashlib.sha256(f"{_PWD_PEPPER}:{salt}:{plain}".encode("utf-8")).hexdigest()
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def create_token(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])


class LoginRequest(BaseModel):
    username: str
    password: str


class OrgCreate(BaseModel):
    name: str


class EngineerCreate(BaseModel):
    full_name: str
    email: str
    org_id: str
    password: str = "engineer123"


class AccessUpdate(BaseModel):
    user_id: str
    active: bool = False
    read: bool = False
    write: bool = False
    delete: bool = False


class Balloon(BaseModel):
    balloon: int
    page: int
    text: str
    x: float
    y: float
    width: float = 20
    height: float = 12
    category: str = ""
    pattern: str = ""
    bx: float | None = None
    by: float | None = None


class BalloonUpdate(BaseModel):
    balloons: list[Balloon]


class MeasureCol(BaseModel):
    id: str
    label: str
    values: dict[str, str] = Field(default_factory=dict)


class ReportRow(BaseModel):
    sno: int
    balloon: int
    reference: str = ""
    nominal: str = ""
    tol_low: str = ""
    tol_high: str = ""
    instrument: str = ""
    unit: str = "mm"
    instr_id: str = ""
    measured: list[str] = Field(default_factory=lambda: ["", "", ""])
    remarks: str = ""


class ReportPayload(BaseModel):
    part_number: str = ""
    part_name: str = ""
    revision: str = ""
    material: str = ""
    mass: str = ""
    finish: str = ""
    rows: list[ReportRow]
    measure_columns: list[MeasureCol] = Field(default_factory=list)
