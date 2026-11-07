"""SQLAlchemy engine / session + DB bootstrap."""

from __future__ import annotations

import time

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

USE_POSTGRES = False
engine = None
SessionLocal = None
LAST_PG_ERROR = ""


class Base(DeclarativeBase):
    pass


def _normalize_pg_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url and "channel_binding=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}channel_binding=disable"
    return url


def _candidate_urls() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (settings.license_database_url, settings.database_url):
        url = _normalize_pg_url(raw)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _try_connect(attempts: int = 3, connect_timeout: int = 20) -> bool:
    global engine, SessionLocal, USE_POSTGRES, LAST_PG_ERROR
    urls = _candidate_urls()
    if not urls:
        LAST_PG_ERROR = "DATABASE_URL is empty"
        return False
    # Retry briefly during an AWS Postgres restart or network hiccup before
    # falling back to the per-instance JSON store. This avoids false startup
    # failures for provisioned accounts and licenses.
    last_err: Exception | None = None
    for url in urls:
        for attempt in range(1, attempts + 1):
            try:
                eng = create_engine(
                    url,
                    pool_pre_ping=True,
                    pool_size=3,
                    max_overflow=2,
                    pool_recycle=300,
                    connect_args={"connect_timeout": connect_timeout},
                )
                with eng.connect() as conn:
                    conn.exec_driver_sql("select 1")
                engine = eng
                SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
                USE_POSTGRES = True
                LAST_PG_ERROR = ""
                return True
            except Exception as e:
                last_err = e
                LAST_PG_ERROR = f"{e.__class__.__name__}: {e}"
                if attempt < attempts:
                    time.sleep(2 * attempt)

    print(f"[smorx] PostgreSQL unavailable ({last_err.__class__.__name__ if last_err else 'error'}); using local JSON store.")
    USE_POSTGRES = False
    engine = None
    SessionLocal = None
    return False


def ensure_postgres() -> bool:
    """Keep using Postgres if it is up; reconnect after a startup fallback or dropped pool."""
    global USE_POSTGRES, engine, SessionLocal, LAST_PG_ERROR
    if USE_POSTGRES and SessionLocal is not None:
        if engine is None:
            return True
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("select 1")
            return True
        except Exception:
            try:
                engine.dispose()
            except Exception:
                pass
            USE_POSTGRES = False
            engine = None
            SessionLocal = None
    if not _try_connect(attempts=1, connect_timeout=3):
        return False
    try:
        from app.core.security import hash_password

        _create_schema_and_seed(hash_password)
    except Exception as exc:
        LAST_PG_ERROR = f"schema sync failed: {exc.__class__.__name__}: {exc}"
        print(f"[smorx] PostgreSQL connected but schema sync failed ({exc.__class__.__name__}).")
        return False
    return True


def ensure_commercial_schema() -> None:
    if not USE_POSTGRES or SessionLocal is None:
        return
    alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_id VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS install_password_hash TEXT",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS require_device_approval BOOLEAN DEFAULT TRUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS product_code VARCHAR(64) DEFAULT 'autoballoon'",
        "UPDATE customers SET product_code = 'autoballoon' WHERE product_code IS NULL OR product_code = ''",
        "ALTER TABLE installations ADD COLUMN IF NOT EXISTS hostname VARCHAR(255)",
        """
        CREATE TABLE IF NOT EXISTS approved_devices (
            id VARCHAR(64) PRIMARY KEY,
            customer_id VARCHAR(64) NOT NULL REFERENCES customers(customer_id),
            device_id VARCHAR(255) NOT NULL,
            hostname VARCHAR(255) NOT NULL,
            status VARCHAR(32) DEFAULT 'Pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            activated_at TIMESTAMPTZ,
            UNIQUE (customer_id, device_id)
        )
        """,
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
    with SessionLocal() as db:
        for stmt in alters:
            try:
                db.execute(text(stmt))
                db.commit()
            except Exception:
                db.rollback()


_SCHEMA_LOCK_ID = 864_210_001


def init_db(hash_password) -> str:
    """Create tables / seed admin. Returns 'postgres' or 'json'."""
    # Import models so they register on Base.metadata
    from app.models import ApprovedDevice, Customer, Installation, Job, Organization, User  # noqa: F401

    if _try_connect():
        assert engine is not None and SessionLocal is not None
        # Several uvicorn workers start at once; serialise schema creation and the
        # admin seed or concurrent CREATE TABLE / INSERT fail and kill a worker.
        with engine.connect() as lock_conn:
            lock_conn.exec_driver_sql(f"SELECT pg_advisory_lock({_SCHEMA_LOCK_ID})")
            try:
                _create_schema_and_seed(hash_password)
            finally:
                lock_conn.exec_driver_sql(f"SELECT pg_advisory_unlock({_SCHEMA_LOCK_ID})")
                lock_conn.commit()
        return "postgres"

    from app import local_store

    local_store.ensure_seed(hash_password)
    return "json"


def _create_schema_and_seed(hash_password) -> None:
    from app.models import User

    assert engine is not None and SessionLocal is not None
    Base.metadata.create_all(bind=engine)
    ensure_commercial_schema()
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.email == settings.superadmin_email))
        if admin is None:
            db.add(
                User(
                    id="u-admin",
                    username=settings.superadmin_username,
                    email=settings.superadmin_email,
                    full_name=settings.superadmin_name,
                    password_hash=hash_password(settings.superadmin_password),
                    role="super_admin",
                    org_id=None,
                    customer_id=None,
                    active=True,
                    perm_read=True,
                    perm_write=True,
                    perm_delete=True,
                )
            )
            db.commit()
