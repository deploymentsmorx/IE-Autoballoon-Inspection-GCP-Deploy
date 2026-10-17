from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization: Mapped[str] = mapped_column(String(255), nullable=False)
    license_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active")
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id"), nullable=True
    )
    install_password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    require_device_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # autoballoon | costsheet — which desktop product this license activates
    product_code: Mapped[str] = mapped_column(String(64), default="autoballoon")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
