from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Job(Base):
    """Ballooning job metadata — PDF bytes live in S3 (or local fallback)."""

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("customers.customer_id"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True
    )
    installation_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("installations.installation_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    part_number: Mapped[str] = mapped_column(String(120), default="")
    part_name: Mapped[str] = mapped_column(String(255), default="")
    revision: Mapped[str] = mapped_column(String(64), default="")
    page_count: Mapped[int] = mapped_column(default=1)
    s3_input_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# Design-doc alias
ProcessingJob = Job
