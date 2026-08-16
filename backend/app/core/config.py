"""Application settings (env-driven)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # backend/
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / "static"


class Settings:
    database_url: str = (os.getenv("DATABASE_URL") or "").strip()
    jwt_secret: str = os.getenv("JWT_SECRET", "smorx-dev-secret-change-in-prod")
    jwt_algorithm: str = "HS256"
    pwd_pepper: str = os.getenv("PWD_PEPPER", "smorx-pepper")
    aws_access_key_id: str = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    aws_secret_access_key: str = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    aws_s3_bucket: str = (os.getenv("AWS_S3_BUCKET") or "").strip()
    aws_region: str = (os.getenv("AWS_REGION") or "us-east-2").strip()
    aws_s3_prefix: str = (os.getenv("AWS_S3_PREFIX") or "autoballoon").strip().strip("/")


settings = Settings()
