"""
File storage: AWS S3 for PDFs when configured, else local disk fallback.

  Client → FastAPI → PostgreSQL (metadata) + S3 (input/output PDFs)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # backend/
load_dotenv(ROOT / ".env")

AWS_ACCESS_KEY_ID = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
AWS_SECRET_ACCESS_KEY = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
AWS_S3_BUCKET = (os.getenv("AWS_S3_BUCKET") or "autoballoon-storage-smorx-prod").strip()
AWS_REGION = (os.getenv("AWS_REGION") or "us-east-1").strip()
# Empty prefix = store at bucket root: s3://autoballoon-storage-smorx-prod/{user}/{job}/...
S3_PREFIX = (os.getenv("AWS_S3_PREFIX") or "").strip().strip("/")
AWS_S3_URL = (
    os.getenv("AWS_S3_URL")
    or f"https://{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com"
).strip().rstrip("/")

# Local fallback root (same jobs folder used today)
LOCAL_ROOT = ROOT / "data" / "jobs"


def s3_enabled() -> bool:
    """S3 is on when a bucket is set. Keys are optional (EC2 IAM role / default chain)."""
    return bool(AWS_S3_BUCKET)


def _client():
    import boto3

    kwargs = {"region_name": AWS_REGION}
    # Pass explicit keys only when both are set. Empty strings would block the IAM role chain.
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def sanitize_username(username: str) -> str:
    """Safe S3 path segment from login username."""
    s = (username or "").strip().lower()
    s = re.sub(r"[^\w.-]", "_", s, flags=re.ASCII)
    s = re.sub(r"_+", "_", s).strip("._")
    return s or "unknown"


def object_key(job_id: str, name: str, *, username: str | None = None) -> str:
    """Canonical S3 key / relative local path for a job file."""
    safe = Path(name).name.replace("\\", "/").lstrip("/")
    user_seg = sanitize_username(username) if username else None
    parts = [p for p in (S3_PREFIX, user_seg, job_id, safe) if p]
    return "/".join(parts)


def put_bytes(
    job_id: str,
    name: str,
    data: bytes,
    content_type: str = "application/pdf",
    *,
    username: str | None = None,
) -> str:
    """
    Store file bytes. Returns storage key (S3 key or local relative path).
    """
    key = object_key(job_id, name, username=username)
    if s3_enabled():
        _client().put_object(
            Bucket=AWS_S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key
    # Local fallback
    dest = LOCAL_ROOT / job_id / Path(name).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return key


def put_file(
    job_id: str,
    name: str,
    path: Path,
    content_type: str = "application/pdf",
    *,
    username: str | None = None,
) -> str:
    return put_bytes(job_id, name, path.read_bytes(), content_type=content_type, username=username)


def get_bytes(key: str) -> bytes:
    """Load file bytes from S3 or local fallback."""
    if s3_enabled() and not key.startswith("local:"):
        try:
            obj = _client().get_object(Bucket=AWS_S3_BUCKET, Key=key)
            return obj["Body"].read()
        except Exception:
            # Fall through to local if key was written before S3 was enabled
            pass
    # key format: prefix/username/job_id/filename OR prefix/job_id/filename (legacy)
    parts = key.replace("\\", "/").split("/")
    if len(parts) >= 2:
        job_id, fname = parts[-2], parts[-1]
        local = LOCAL_ROOT / job_id / fname
        if local.exists():
            return local.read_bytes()
    raise FileNotFoundError(f"Storage object not found: {key}")


def get_to_path(key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(get_bytes(key))
    return dest


def storage_mode() -> str:
    return "s3" if s3_enabled() else "local"


def s3_base_url() -> str:
    """Public bucket base URL (objects live at {base}/{key})."""
    return AWS_S3_URL


def object_url(key: str) -> str:
    """Full HTTPS URL for a stored object key."""
    safe = key.replace("\\", "/").lstrip("/")
    return f"{AWS_S3_URL}/{safe}"
