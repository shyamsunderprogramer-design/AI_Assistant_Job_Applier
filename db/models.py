"""SQLAlchemy models. The DB is the source of truth for what we've seen."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Application status vocabulary.
#
# This lives here, not in excel/tracker.py, because three things now write it —
# the scorer, the lifecycle reconciler, and the Excel export — and a status one
# of them sets that another does not recognise is a silent bug. One list.
#
# SYSTEM_STATUSES is the subset the pipeline is allowed to OVERWRITE: they
# record no human decision. Everything else ("Applied", "Rejected", ...) is the
# user's judgement and is never touched by code (README.md §C7).
# ---------------------------------------------------------------------------
DEFAULT_STATUS = "Not Applied"
REVIEW_STATUS = "Manual Review"
CLOSED_STATUS = "Closed"

STATUS_VALUES = [
    DEFAULT_STATUS,
    REVIEW_STATUS,
    CLOSED_STATUS,
    "Applied",
    "Interviewing",
    "Rejected",
    "Offer",
    "Skipped",
]

SYSTEM_STATUSES = {"", DEFAULT_STATUS, REVIEW_STATUS, CLOSED_STATUS}


class Company(Base):
    """A company and the ATS it uses. Populated by config seeds or discovery."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("source", "slug", name="uq_company_source_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # greenhouse | lever
    board_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), default="config")  # config | discovery | csv
    active: Mapped[bool] = mapped_column(default=True)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Company {self.source}:{self.slug}>"


class Job(Base):
    """A discovered posting. Identity is (source, company_slug, external_id)."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "source", "company_slug", "external_id", name="uq_job_source_company_extid"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    company: Mapped[str] = mapped_column(String(255), index=True)
    company_slug: Mapped[str] = mapped_column(String(255), index=True)
    external_id: Mapped[str] = mapped_column(String(128))

    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_url: Mapped[str] = mapped_column(String(1024))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Posting lifecycle. A board stops listing a role once it is filled or
    # pulled, so "still on the board" has to be tracked explicitly — otherwise
    # the tracker keeps recommending roles that no longer exist, and a tailored
    # resume gets spent on one. `last_seen_at` only advances on a SUCCESSFUL
    # scrape of that company, so an outage never looks like a closure.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_open: Mapped[bool] = mapped_column(default=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Phase 2/3 fields, unused in Phase 1
    status: Mapped[str] = mapped_column(String(32), default=DEFAULT_STATUS)
    ats_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    exported_to_excel: Mapped[bool] = mapped_column(default=False)

    @staticmethod
    def make_content_hash(title: str, location: str | None, description: str | None) -> str:
        """Detects an edited posting without creating a duplicate row."""
        payload = "\x1f".join([title or "", location or "", description or ""])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Job {self.company}: {self.title}>"


class ProbeLog(Base):
    """One row per (source, slug) ever probed by discovery.

    Discovery is the one part of this tool that makes a request per GUESS
    rather than per known company, so a 5,000-name list is tens of thousands of
    requests, most of them misses. Without a memory of what has already been
    asked, every re-run re-asks every board the same dead questions.

    That makes this cache a politeness feature as much as a speed one
    (README.md §C5): the cheapest request is the one never sent. It also makes
    a long discovery run resumable — an interrupted run has already persisted
    everything it learned.
    """

    __tablename__ = "probe_log"
    __table_args__ = (UniqueConstraint("source", "slug", name="uq_probe_source_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    slug: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    found: Mapped[bool] = mapped_column(default=False)
    probed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ProbeLog {self.source}:{self.slug} found={self.found}>"


class ScrapeLog(Base):
    """One row per company scrape attempt. Failures are logged, never raised."""

    __tablename__ = "scrape_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    company_slug: Mapped[str] = mapped_column(String(255), index=True)
    ok: Mapped[bool] = mapped_column(default=True)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_kept: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
