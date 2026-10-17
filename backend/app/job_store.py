"""Job metadata persistence in Postgres (+ local meta.json mirror for previews)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.core import database as dbmod
from app.models import Job
from app.postgres import job_to_dict
from app.utils import file_handler as storage


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_job_schema() -> None:
    """Add new columns on existing Postgres DBs (create_all does not ALTER)."""
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return
    alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_id VARCHAR(64)",
        """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id VARCHAR(64) PRIMARY KEY,
            organization VARCHAR(255) NOT NULL,
            license_key VARCHAR(120) UNIQUE NOT NULL,
            status VARCHAR(32) DEFAULT 'Active',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS installations (
            installation_id VARCHAR(64) PRIMARY KEY,
            customer_id VARCHAR(64) NOT NULL REFERENCES customers(customer_id),
            device_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) DEFAULT 'Active',
            activated_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id VARCHAR(64) PRIMARY KEY,
            customer_id VARCHAR(64) REFERENCES customers(customer_id),
            user_id VARCHAR(64) REFERENCES users(id),
            installation_id VARCHAR(64) REFERENCES installations(installation_id),
            status VARCHAR(32) DEFAULT 'uploaded',
            part_number VARCHAR(120) DEFAULT '',
            part_name VARCHAR(255) DEFAULT '',
            revision VARCHAR(64) DEFAULT '',
            page_count INTEGER DEFAULT 1,
            s3_input_key TEXT,
            s3_output_key TEXT,
            meta_json TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
    ]
    with dbmod.SessionLocal() as db:
        for stmt in alters:
            try:
                db.execute(text(stmt))
                db.commit()
            except Exception:
                db.rollback()


def upsert_job_from_meta(meta: dict, *, input_key: str | None = None, output_key: str | None = None) -> dict | None:
    """Mirror job meta.json into Postgres jobs table."""
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return None
    job_id = str(meta.get("id") or "")
    if not job_id:
        return None
    with dbmod.SessionLocal() as db:
        row = db.get(Job, job_id)
        if row is None:
            row = Job(job_id=job_id)
            db.add(row)
        row.user_id = meta.get("owner")
        row.customer_id = meta.get("customer_id")
        row.installation_id = meta.get("installation_id")
        row.status = str(meta.get("status") or "uploaded")
        row.part_number = str(meta.get("part_number") or "")
        row.part_name = str(meta.get("part_name") or "")
        row.revision = str(meta.get("revision") or "")
        row.page_count = int(meta.get("page_count") or 1)
        if input_key:
            row.s3_input_key = input_key
        if output_key:
            row.s3_output_key = output_key
        # Keep a compact JSON snapshot (balloons + files) for recovery
        snap = {
            "files": meta.get("files") or [],
            "balloons": meta.get("balloons") or [],
            "active_file": meta.get("active_file", 0),
            "pass_log": meta.get("pass_log"),
        }
        row.meta_json = json.dumps(snap)
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return job_to_dict(row)


def get_job(job_id: str) -> dict | None:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return None
    with dbmod.SessionLocal() as db:
        row = db.get(Job, job_id)
        return job_to_dict(row) if row else None


def list_jobs_for_user(user_id: str, limit: int = 50) -> list[dict]:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return []
    with dbmod.SessionLocal() as db:
        rows = db.scalars(
            select(Job).where(Job.user_id == user_id).order_by(Job.created_at.desc()).limit(limit)
        ).all()
        return [job_to_dict(r) for r in rows]


def set_job_status(job_id: str, status: str, *, output_key: str | None = None) -> None:
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None:
        return
    with dbmod.SessionLocal() as db:
        row = db.get(Job, job_id)
        if row is None:
            return
        row.status = status
        if output_key:
            row.s3_output_key = output_key
        row.updated_at = _now()
        db.commit()


def _resolve_owner_username(meta: dict) -> str:
    """Username folder segment for S3 keys (per-client drawing storage)."""
    raw = str(meta.get("owner_username") or "").strip()
    if raw:
        return raw
    owner_id = str(meta.get("owner") or "").strip()
    if owner_id:
        try:
            from app.services import auth_service

            user = auth_service.get_user(owner_id)
            if user and user.get("username"):
                return str(user["username"])
        except Exception:
            pass
    return "unknown"


def sync_job_files_to_storage(job_id: str, local_dir, meta: dict) -> dict:
    """
    Upload input (and output if present) PDFs to S3 when enabled.
    Updates meta with storage keys and Postgres row.
    """
    files = meta.get("files") or []
    input_key = None
    username = _resolve_owner_username(meta)
    meta["owner_username"] = username
    for f in files:
        path_name = f.get("path")
        if not path_name:
            continue
        local = local_dir / path_name
        if local.exists():
            key = storage.put_file(job_id, path_name, local, username=username)
            f["storage_key"] = key
            if input_key is None:
                input_key = key
    out_name = meta.get("ballooned_pdf") or "ballooned_0.pdf"
    out_path = local_dir / out_name
    output_key = None
    if out_path.exists():
        output_key = storage.put_file(job_id, out_name, out_path, username=username)
        meta["s3_output_key"] = output_key
    if input_key:
        meta["s3_input_key"] = input_key
    meta["storage"] = storage.storage_mode()
    upsert_job_from_meta(meta, input_key=input_key, output_key=output_key)
    return meta
