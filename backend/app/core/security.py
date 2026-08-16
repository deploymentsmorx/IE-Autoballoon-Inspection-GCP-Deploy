"""Password hashing + JWT helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings


def hash_password(plain: str) -> str:
    """Hash password for DB storage. Never store plain text."""
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(
        f"{settings.pwd_pepper}:{salt}:{plain}".encode("utf-8")
    ).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(plain: str, hashed: str) -> bool:
    try:
        kind, salt, digest = hashed.split("$", 2)
        if kind != "sha256":
            return False
        check = hashlib.sha256(
            f"{settings.pwd_pepper}:{salt}:{plain}".encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def create_token(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
