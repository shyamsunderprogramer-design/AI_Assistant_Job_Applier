"""The daily loop: scrape, retire, rank, export — one command, one digest.

Everything this runs already existed as a separate command. That was the
problem: a five-command routine gets run once, enthusiastically, and then never
again. The tool only pays off if it runs every day, so the friction of
remembering the order is the thing most worth removing.

Two design rules, both learned from the phases before this one:

  A PARTIAL RUN STILL REPORTS. A stage that fails must not take the digest with
  it — the scrape can die and the ranking of yesterday's jobs is still worth
  seeing. Each stage is caught, recorded, and the next one runs.

  THE DIGEST IS THE PRODUCT. Not the rows in the database. If the user does not
  read it, the run did nothing for them, so it leads with what CHANGED rather
  than with totals that look identical every morning.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from db.models import Job, utcnow
from db.session import get_session
from scraper.lifecycle import as_utc, stale_companies

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    name: str
    ok: bool = True
    detail: str = ""
    error: str = ""
    seconds: float = 0.0


@dataclass
class DailyReport:
    """What one daily run changed. The digest is rendered from this."""

    started_at: datetime = field(default_factory=utcnow)
    stages: list[StageResult] = field(default_factory=list)

    companies: int = 0
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_closed: int = 0
    jobs_reopened: int = 0
    scrape_failures: int = 0

    scored: int = 0
    exported: int = 0

    new_jobs: list[tuple[str, str, float, str]] = field(default_factory=list)
    closed_jobs: list[tuple[str, str]] = field(default_factory=list)
    stale: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(stage.ok for stage in self.stages)

    @property
    def failed_stages(self) -> list[StageResult]:
        return [s for s in self.stages if not s.ok]

    def exit_code(self) -> int:
        """0 all good, 1 a stage failed, 2 nothing ran at all.

        Meaningful codes are what let this sit behind cron or launchd without a
        human reading the output every morning.
        """
        if not self.stages:
            return 2
        return 0 if self.ok else 1


def _run_stage(report: DailyReport, name: str, fn) -> object:
    """Run one stage, record it, and never let it abort the run."""
    started = time.monotonic()
    try:
        result = fn()
        report.stages.append(
            StageResult(name=name, ok=True, seconds=time.monotonic() - started)
        )
        return result
    except Exception as exc:
        log.exception("Daily stage %r failed", name)
        report.stages.append(
            StageResult(
                name=name, ok=False, error=f"{type(exc).__name__}: {exc}",
                seconds=time.monotonic() - started,
            )
        )
        return None


def run_daily(cfg, skip_scrape: bool = False, since_hours: int = 24) -> DailyReport:
    """Scrape, retire what vanished, re-rank, and push to the sheet."""
    report = DailyReport()
    cutoff = utcnow() - timedelta(hours=since_hours)

    # -- 1. scrape (also closes postings that vanished) --------------------
    if not skip_scrape:
        def _scrape():
            from scraper.runner import run_scrape, sync_seed_companies

            sync_seed_companies(cfg)
            return run_scrape(cfg)

        summary = _run_stage(report, "scrape", _scrape)
        if summary is not None:
            report.companies = summary.companies_attempted
            report.jobs_seen = summary.jobs_seen
            report.jobs_new = summary.jobs_new
            report.jobs_closed = summary.jobs_closed
            report.jobs_reopened = summary.jobs_reopened
            report.scrape_failures = summary.companies_failed
            report.stages[-1].detail = (
                f"{summary.jobs_new} new, {summary.jobs_closed} closed, "
                f"{summary.companies_failed} boards failed"
            )

    # -- 2. rank ------------------------------------------------------------
    def _score():
        from resume.pipeline import score_jobs

        return score_jobs(cfg)

    outcomes = _run_stage(report, "score", _score)
    if outcomes is not None:
        report.scored = len(outcomes)
        report.stages[-1].detail = f"{len(outcomes)} newly scored"

    # -- 3. export ----------------------------------------------------------
    def _export():
        from excel.tracker import export_jobs

        return export_jobs(cfg, only_new=True)

    exported = _run_stage(report, "export", _export)
    if exported is not None:
        appended, updated, total = exported
        report.exported = total
        report.stages[-1].detail = f"{appended} appended, {updated} updated"

    # -- 4. gather what changed, for the digest -----------------------------
    def _collect():
        with get_session() as session:
            fresh = (
                session.query(Job)
                .filter(Job.is_open.is_(True))
                .order_by(Job.ats_match_score.desc())
                .all()
            )
            report.new_jobs = [
                (j.company, j.title, j.ats_match_score or 0.0, j.application_url)
                for j in fresh
                if as_utc(j.found_at) and as_utc(j.found_at) >= cutoff
            ]
            gone = (
                session.query(Job)
                .filter(Job.is_open.is_(False), Job.closed_at.isnot(None))
                .all()
            )
            report.closed_jobs = [
                (j.company, j.title) for j in gone
                if as_utc(j.closed_at) and as_utc(j.closed_at) >= cutoff
            ]
        report.stale = stale_companies(int(cfg.get("limits.stale_company_warn_days", 7) or 0))
        return True

    _run_stage(report, "digest", _collect)
    return report


def render_digest(report: DailyReport, cfg, top: int = 10) -> str:
    """The digest, as plain text. This is what the user actually reads."""
    threshold = float(cfg.get("resume.min_score", 0.6))
    when = report.started_at.strftime("%a %d %b %Y, %H:%M")
    lines = [f"JOB DIGEST — {when}", "=" * 58, ""]

    # Lead with what changed. Totals look identical every morning; changes do not.
    if report.new_jobs:
        strong = [j for j in report.new_jobs if j[2] >= threshold]
        lines.append(f"{len(report.new_jobs)} NEW since yesterday"
                     + (f" — {len(strong)} worth a look" if strong else ""))
        lines.append("")
        for company, title, score, url in report.new_jobs[:top]:
            mark = "*" if score >= threshold else " "
            lines.append(f"  {score:>4.0%}{mark} {company[:18]:<20} {title[:46]}")
            if score >= threshold:
                lines.append(f"        {url}")
        if len(report.new_jobs) > top:
            lines.append(f"  ... and {len(report.new_jobs) - top} more in the sheet")
    else:
        lines.append("No new postings since yesterday.")
    lines.append("")

    if report.closed_jobs:
        lines.append(f"{len(report.closed_jobs)} CLOSED — gone from the board, stopped being ranked")
        for company, title in report.closed_jobs[:5]:
            lines.append(f"       {company[:18]:<20} {title[:46]}")
        if len(report.closed_jobs) > 5:
            lines.append(f"  ... and {len(report.closed_jobs) - 5} more")
        lines.append("")

    lines.append("-" * 58)
    lines.append(
        f"  {report.companies} boards scraped   {report.jobs_seen:,} postings seen   "
        f"{report.exported} rows updated"
    )

    if report.scrape_failures:
        lines.append(f"  {report.scrape_failures} boards failed — `main.py failures` for why")

    if report.stale:
        lines.append(
            f"  {len(report.stale)} companies not scraped recently — their jobs may be stale"
        )

    if report.failed_stages:
        lines.append("")
        lines.append("STAGES THAT FAILED (the rest still ran):")
        for stage in report.failed_stages:
            lines.append(f"  {stage.name}: {stage.error}")

    return "\n".join(lines)
