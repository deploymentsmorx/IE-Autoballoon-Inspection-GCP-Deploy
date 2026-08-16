"""SQLAlchemy engine / session + DB bootstrap."""

from __future__ import annotations

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

USE_POSTGRES = False
engine = None
SessionLocal = None


class Base(DeclarativeBase):
    pass


def _try_connect() -> bool:
    global engine, SessionLocal, USE_POSTGRES
    url = (settings.database_url or "").strip()
    if not url:
        return False
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if "channel_binding=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}channel_binding=disable"
    try:
        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            pool_recycle=300,
            connect_args={"connect_timeout": 5},
        )
        with eng.connect() as conn:
            conn.exec_driver_sql("select 1")
        engine = eng
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        USE_POSTGRES = True
        return True
    except Exception as e:
        print(f"[smorx] Neon unavailable ({e.__class__.__name__}); using local JSON store.")
        USE_POSTGRES = False
        engine = None
        SessionLocal = None
        return False


def ensure_commercial_schema() -> None:
    if not USE_POSTGRES or SessionLocal is None:
        return
    alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_id VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS install_password_hash TEXT",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS require_device_approval BOOLEAN DEFAULT TRUE",
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


def init_db(hash_password) -> str:
    """Create tables / seed admin. Returns 'neon' or 'json'."""
    # Import models so they register on Base.metadata
    from app.models import ApprovedDevice, Customer, Installation, Job, Organization, User  # noqa: F401

    if _try_connect():
        assert engine is not None and SessionLocal is not None
        Base.metadata.create_all(bind=engine)
        ensure_commercial_schema()
        with SessionLocal() as db:
            admin = db.scalar(select(User).where(User.email == "admin@smorx.ai"))
            if admin is None:
                db.add(
                    User(
                        id="u-admin",
                        username="admin",
                        email="admin@smorx.ai",
                        full_name="Super Admin",
                        password_hash=hash_password("admin123"),
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
        return "neon"

    from app import local_store

    local_store.ensure_seed(hash_password)
    return "json"
