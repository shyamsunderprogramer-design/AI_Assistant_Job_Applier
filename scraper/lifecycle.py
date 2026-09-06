"""Posting lifecycle — what is still on the board, and what quietly vanished.

A board drops a role the day it is filled, so "still open" has to be tracked
explicitly. Without it the tracker keeps ranking dead postings, spends a
tailoring call on them, and sends the user to a 404.

The dangerous version of this feature closes jobs on bad evidence. Three rules
keep it safe:

  1. Only a SUCCESSFUL fetch may close anything. A 500, a timeout, or a robots
     block is an outage, not a hiring freeze — the runner simply does not call
     in that case, so a failed company's jobs cannot be touched.

  2. A board returning ZERO postings closes nothing by default. An empty 200 is
     indistinguishable from a board that broke, and the blast radius of being
     wrong is every job for that company. Opt in with
     `limits.close_on_empty_board` if a company really did empty out.

  3. Closure is judged against EVERY posting the board returned, not the ones
     that passed the keyword filters. A stored job whose title was edited out
     of the filters is still on the board — closing it would be a lie.

Reopening is symmetric: a posting that comes back clears `closed_at` and its
system-set "Closed" status, the same way the scorer's "Manual Review" flag
clears when a job later beats the threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from db.models import CLOSED_STATUS, DEFAULT_STATUS, SYSTEM_STATUSES, Company, Job, utcnow
from db.session import get_session

log = logging.getLogger(__name__)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise a datetime to tz-aware UTC.

    `utcnow()` produces tz-AWARE datetimes, but the columns are plain
    `DateTime`, so SQLite hands them back NAIVE. Comparing the two raises
    TypeError, which would take out any freshness check the moment it touched
    a stored timestamp. Every comparison here goes through this first.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class LifecycleResult:
    """What one board reconciliation changed."""

    seen: int = 0
    closed: int = 0
    reopened: int = 0
    skipped_empty: bool = False


def reconcile_board(
    source: str,
    company_slug: str,
    live_external_ids,
    *,
    close_on_empty_board: bool = False,
    now=None,
) -> LifecycleResult:
    """Reconcile stored jobs for one company against a successful board fetch.

    `live_external_ids` must be every posting the board returned — pre-filter.
    Call this ONLY after a fetch that succeeded.
    """
    now = as_utc(now) or utcnow()
    live = {str(x) for x in live_external_ids}
    result = LifecycleResult()

    if not live and not close_on_empty_board:
        # Rule 2. Cheap to be wrong in this direction; expensive in the other.
        result.skipped_empty = True
        log.warning(
            "%s/%s returned no postings — closing nothing. "
            "Set limits.close_on_empty_board: true if that is genuinely correct.",
            source, company_slug,
        )
        return result

    with get_session() as session:
        rows = session.query(Job).filter_by(source=source, company_slug=company_slug).all()

        for job in rows:
            if job.external_id in live:
                job.last_seen_at = now
                result.seen += 1
                if not job.is_open:
                    job.is_open = True
                    job.closed_at = None
                    if job.status == CLOSED_STATUS:
                        job.status = DEFAULT_STATUS
                    job.exported_to_excel = False
                    result.reopened += 1
            elif job.is_open:
                job.is_open = False
                job.closed_at = now
                # Never overwrite a human decision — "Applied" outlives the
                # posting, and is exactly the row the user needs to keep.
                if job.status in SYSTEM_STATUSES:
                    job.status = CLOSED_STATUS
                job.exported_to_excel = False
                result.closed += 1

    if result.closed or result.reopened:
        log.info(
            "%s/%s lifecycle: %d closed, %d reopened, %d still open",
            source, company_slug, result.closed, result.reopened, result.seen,
        )
    return result


@dataclass
class StaleCompany:
    """A company whose data is old enough that its jobs can't be trusted."""

    source: str
    slug: str
    name: str
    last_scraped_at: object | None
    days: float | None  # None = never successfully scraped


def stale_companies(warn_days: int, now=None) -> list[StaleCompany]:
    """Active companies not successfully scraped within `warn_days`.

    Silence is the failure mode this catches: a board that stopped being
    scraped keeps its jobs marked open forever, because closure only ever
    happens on a successful fetch. Nothing looks wrong — the rows just rot.
    """
    if warn_days <= 0:
        return []

    now = as_utc(now) or utcnow()
    cutoff = now - timedelta(days=warn_days)
    stale: list[StaleCompany] = []

    with get_session() as session:
        for row in session.query(Company).filter(Company.active.is_(True)).order_by(Company.id):
            last = as_utc(row.last_scraped_at)
            if last is None:
                stale.append(StaleCompany(row.source, row.slug, row.name, None, None))
            elif last < cutoff:
                stale.append(
                    StaleCompany(row.source, row.slug, row.name, last, (now - last).days)
                )
    return stale
