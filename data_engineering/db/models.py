"""SQLAlchemy models. The DB is the source of truth for what we've seen."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Application status vocabulary.
#
# This lives here, not in data_engineering/excel/tracker.py, because three things now write it —
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
    # The employer's own page for this job -- the posting on their careers site, or the careers site
    # itself -- added by the person on the job's page (25 Oct 2026), for a posting that came through a
    # job site such as Adzuna. Its own column: a fetch rewrites application_url, never this one.
    employer_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # What that link is (backend/core/joblinks.py): "posting", this job's own page, applied on; or
    # "careers", the careers site, a link to look from and never taken for the form.
    employer_url_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
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

    # When an application actually went out. `status = "Applied"` says that it
    # did; this says when, which is what lets applications to one employer be
    # spaced rather than merely capped. Null on every row that predates the
    # apply step, and null is read as "long ago" so old rows never block.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # What the match score was actually measured against. "full" means a real
    # job description; "title" means there was none and only the title and a
    # few metadata lines were available. A job-alert email carries about 217
    # characters where a scraped posting carries 6,758, so the two scores are
    # not on the same scale and must not be displayed as though they were --
    # Zoom scoring 23% did not mean it was a weak match, it meant nobody could
    # tell. Defaults to "full", which is true of every row that predates this.
    score_basis: Mapped[str] = mapped_column(String(16), default="full")

    # Facts the posting states about itself, parsed from its own text by
    # backend/core/jobfields.py. NULL means the posting did not say — never "unknown" as a
    # word, or every filter would have to parse English back out. These are
    # derived from the description, which content_hash covers, so a re-scrape
    # only refreshes them when the text itself changed; `reparse` is what
    # backfills existing rows and re-derives after a parser fix.
    workplace: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # What kind of job: full-time, contract, contract-to-hire, part-time, temporary, internship
    # (backend/core/jobfields.job_type), from the title, the board's own word for it and the text.
    job_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The board's own word ("Full-time" from Lever, "FullTime" from Ashby), kept for re-reading.
    stated_job_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Set by the person on the job's page: re-reading leaves job_type alone.
    job_type_manual: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    experience_min_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_max_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Who can take the job, as the posting states it (backend/core/eligibility.py).
    clearance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    clearance_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    polygraph: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    citizenship: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sponsorship: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Set by the person after reading the posting: re-parsing leaves these alone.
    eligibility_manual: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    salary_period: Mapped[str | None] = mapped_column(String(8), nullable=True)

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


# ---------------------------------------------------------------------------
# The jobs the person chose to apply to, on their way through (backend/apply/execution.py).
# Added 24 Oct 2026: kept here rather than in a file so a crash, a restart of the app or of the
# Mac never loses where each one stands, and two processes never take the same one.
# ---------------------------------------------------------------------------

FINISHED_STATES = ("applied", "failed", "skipped", "cancelled")


class ApplyHistoryOutbox(Base):
    """One immutable application history payload, committed with the application's state.

    Export acknowledgement may lag a crash; event_id deduplicates the JSONL export.
    Existing applications are never backfilled into this table.
    """

    __tablename__ = "apply_history_outbox"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    payload: Mapped[str] = mapped_column(Text)
    destination: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ApplyItem(Base):
    """One chosen job: its state, what is happening to it, and what it needs from the person."""

    __tablename__ = "apply_items"
    __table_args__ = (
        # One live entry per job. A finished one may be chosen again; one on its way may not be
        # added twice -- the database refuses it, whoever asks.
        Index("uq_apply_items_live_job", "job_id", unique=True,
              sqlite_where=text("state NOT IN ('applied', 'failed', 'skipped', 'cancelled')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)          # what is happening, or why
    action: Mapped[str | None] = mapped_column(Text, nullable=True)        # what the person must do
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)       # how it ended
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)          # cleared from the page, kept here
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Its latest eligibility decision (backend/core/eligibility.Decision, 26 Oct 2026): ELIGIBLE ...
    # UNVERIFIED, and the whole of it -- reasons, the posting's words, unknowns, when, which rules -- as JSON.
    decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


LIVE_RUN = "state IN ('running', 'pausing')"


class ApplyRun(Base):
    """One press of Apply to these: the process going down the list, and whether it should stop."""

    __tablename__ = "apply_runs"
    __table_args__ = (
        # One live run at most: two processes starting together cannot both have one (25 Oct 2026).
        Index("uq_apply_runs_one_live", text(f"({LIVE_RUN})"), unique=True, sqlite_where=text(LIVE_RUN)),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running", index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplyEvent(Base):
    """What happened to a chosen job, in order: the Auto-apply page's activity. Never edited."""

    __tablename__ = "apply_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    state: Mapped[str | None] = mapped_column(String(24), nullable=True)   # the state entered, if any
    message: Mapped[str] = mapped_column(Text, default="")


class ApplyArtifact(Base):
    """A document made ready for one chosen job (26 Oct 2026, Auto Apply v3.0 phase 3): exactly
    which file, its checksum, and the run and attempt that made it ready. A run sends that file's
    content and no other: checked against the checksum just before the form opens.

    state  ready     made ready, not yet superseded
           replaced  a later preparation of the same job made another
           stale     changed or gone since it was made ready: never sent"""

    __tablename__ = "apply_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))                       # resume | cover_letter
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)    # where it came from: "tailored", ...
    # The file it was made from (the job's tailored resume, its cover letter) and that file's checksum then:
    # changed since -- tailored again, edited -- and this one is no longer the one the person means to send.
    origin_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="ready", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
