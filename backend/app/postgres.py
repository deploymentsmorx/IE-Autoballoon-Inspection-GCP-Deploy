"""Neon / Postgres persistence with JSON fallback if Neon is unreachable."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
USE_POSTGRES = False
engine = None
SessionLocal = None


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active")
    trial_ends: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="engineer")
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_read: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_write: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def _try_connect() -> bool:
    global engine, SessionLocal, USE_POSTGRES, DATABASE_URL
    if not DATABASE_URL:
        return False
    # Prefer psycopg3 driver
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    if "channel_binding=" not in DATABASE_URL:
        sep = "&" if "?" in DATABASE_URL else "?"
        DATABASE_URL = f"{DATABASE_URL}{sep}channel_binding=disable"
    try:
        eng = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            pool_recycle=300,
            connect_args={"connect_timeout": 15},
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


def init_db(hash_password) -> str:
    """Create tables / seed admin. Returns 'neon' or 'json'."""
    if _try_connect():
        assert engine is not None and SessionLocal is not None
        Base.metadata.create_all(bind=engine)
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
                        active=True,
                        perm_read=True,
                        perm_write=True,
                        perm_delete=True,
                    )
                )
                db.commit()
        return "neon"

    # JSON fallback
    from . import local_store

    local_store.ensure_seed(hash_password)
    return "json"


def user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "full_name": u.full_name,
        "password_hash": u.password_hash,
        "role": u.role,
        "org_id": u.org_id,
        "active": bool(u.active),
        "permissions": {
            "read": bool(u.perm_read),
            "write": bool(u.perm_write),
            "delete": bool(u.perm_delete),
        },
    }


def org_to_dict(o: Organization, engineers: int = 0) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "tenant_id": o.tenant_id,
        "status": o.status,
        "trial_ends": o.trial_ends.isoformat() if o.trial_ends else "",
        "engineers": engineers,
    }
