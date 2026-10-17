"""Job metadata persistence in Postgres (+ local meta.json mirror)."""

from __future__ import annotations

# Keep implementation in job_store during migration; re-export
from app.job_store import (  # noqa: F401
    ensure_job_schema,
    get_job,
    list_jobs_for_user,
    set_job_status,
    sync_job_files_to_storage,
    upsert_job_from_meta,
)
