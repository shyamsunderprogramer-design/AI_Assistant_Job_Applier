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
from datetime import datetime, timedelta, timezone

from data_engineering.db.models import Job, utcnow
from data_engineering.db.session import get_session
from backend.core.jobage import age_label
from backend.core.jobfields import UNSTATED, experience_label, salary_label, workplace_label


def _facts(job) -> str:
    """The three facts that decide whether a posting is worth opening.

    Only what the posting actually states: a line of "not published" three
    times over is noise, so an unstated field is simply absent.
    """
    parts = [
        workplace_label(job.workplace),
        experience_label(job.experience_min_years, job.experience_max_years),
        salary_label(job.salary_min, job.salary_max,
                     job.salary_currency, job.salary_period),
    ]
    return "  ·  ".join(p for p in parts if p != UNSTATED)
from data_engineering.scraper.lifecycle import as_utc, stale_companies

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
    fetched_by: str = ""        # "the GitHub run" when the boards were read in the cloud

    scored: int = 0
    exported: int = 0

    new_jobs: list[tuple[str, str, float, str, str, str]] = field(default_factory=list)
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


def run_daily(cfg, skip_scrape: bool = False, since_hours: int = 24, cloud: bool = False) -> DailyReport:
    """Scrape, retire what vanished, re-rank, and push to the sheet.

    `cloud`: the scheduled morning. The GitHub run reads the boards early, laptop shut or off;
    its postings are brought in when it has finished, waited for when it is under way, and the
    boards are read here only when there is none to take (devops/cloud_results.py). When the run
    taken is last evening's, the night's postings follow in `run_top_up`. The job feeds
    and alert emails are read here either way: they need this Mac's keys and mailbox."""
    report = DailyReport()
    cutoff = utcnow() - timedelta(hours=since_hours)

    # -- 1a. the GitHub run's postings, on a scheduled morning ----------------
    from_cloud = None
    from devops.cloud_results import set_up as cloud_set_up
    if cloud and not skip_scrape and cloud_set_up():
        def _cloud():
            from devops.cloud_results import bring_in
            return bring_in(say=lambda line: print(f"{datetime.now():%H:%M:%S}  {line}", flush=True))

        from_cloud = _run_stage(report, "cloud", _cloud)
        if from_cloud:
            _from_cloud(report, from_cloud)

    # -- 1. scrape (also closes postings that vanished) --------------------
    if not skip_scrape and not from_cloud:
        def _scrape():
            from data_engineering.scraper.runner import run_scrape, sync_seed_companies

            from devops.probe_runner import paused

            sync_seed_companies(cfg)
            # The board search waits while the boards are read here: one connection, one writer.
            with paused("the job boards are read on this Mac"):
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

    # -- 1b. Adzuna: employers on no board this tool scrapes ------------------
    if not skip_scrape:
        from data_engineering.scraper.adzuna import credentials

        if credentials():
            def _adzuna():
                from data_engineering.scraper.adzuna import run as run_adzuna
                return run_adzuna(cfg)

            found = _run_stage(report, "adzuna", _adzuna)
            if found is not None:
                report.jobs_new += found.new
                report.stages[-1].detail = (f"{found.new} new from {found.calls} searches, "
                                            f"{found.duplicates} already on a board")

    # -- 1c. the job feeds: public APIs, and keyed ones whose keys are set ----
    if not skip_scrape:
        def _feeds():
            from data_engineering.scraper.feeds import run_feeds
            return run_feeds(cfg)

        summaries = _run_stage(report, "feeds", _feeds)
        if summaries is not None:
            added = sum(s.new for s in summaries)
            report.jobs_new += added
            ran = [s for s in summaries if not s.skipped and not s.error]
            failed = [s.label for s in summaries if s.error]
            report.stages[-1].detail = (f"{added} new from {len(ran)} feeds"
                                        + (f"; failed: {', '.join(failed)}" if failed else ""))

    # -- 1d. job-alert emails: Indeed, Dice, Glassdoor, ZipRecruiter, Monster --
    if not skip_scrape:
        import os

        if os.getenv("MAIL_ADDRESS") and os.getenv("MAIL_APP_PASSWORD"):
            def _alerts():
                from data_engineering.scraper.alert_import import fetch_alerts, store
                return store(fetch_alerts(since_days=int(cfg.get("alerts.since_days", 3))))

            stored = _run_stage(report, "alerts", _alerts)
            if stored is not None:
                report.jobs_new += stored[0]
                report.stages[-1].detail = f"{stored[0]} new from alert emails"

    _rank_and_gather(report, cfg, cutoff)
    return report


def _from_cloud(report: DailyReport, got: dict) -> None:
    report.companies = got.get("boards", 0)
    report.jobs_new = got.get("added", 0)
    report.jobs_seen = report.jobs_new + got.get("refreshed", 0)
    report.jobs_closed = got.get("closed", 0)
    report.jobs_reopened = got.get("reopened", 0)
    report.fetched_by = "the GitHub run"
    report.stages[-1].detail = (f"{report.jobs_new} new, {report.jobs_closed} closed, "
                                f"from run {got.get('run')}")


def run_top_up(cfg, since_hours: int = 24) -> DailyReport | None:
    """The night's postings, after the morning run took last evening's GitHub fetch: waited for,
    brought in, ranked and put in the sheet, and the digest written again with them
    (devops/cloud_results.py top_up). None when the morning run asked for none."""
    from devops.cloud_results import TOPUP, set_up, top_up
    if not TOPUP.exists() or not set_up():
        return None
    report = DailyReport()
    got = _run_stage(report, "top-up", lambda: top_up(
        say=lambda line: print(f"{datetime.now():%H:%M:%S}  {line}", flush=True)))
    if not got:
        return report
    _from_cloud(report, got)
    _rank_and_gather(report, cfg, utcnow() - timedelta(hours=since_hours))
    return report


def _rank_and_gather(report: DailyReport, cfg, cutoff) -> None:
    """Rank what is new, put it in the sheet, and gather what changed for the digest."""
    # -- 2. rank ------------------------------------------------------------
    def _score():
        from ml.resume.pipeline import score_jobs

        return score_jobs(cfg)

    outcomes = _run_stage(report, "score", _score)
    if outcomes is not None:
        report.scored = len(outcomes)
        report.stages[-1].detail = f"{len(outcomes)} newly scored"

    # -- 3. export ----------------------------------------------------------
    def _export():
        from data_engineering.excel.tracker import export_jobs

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
            # The age is resolved here, while the row is still in scope: the
            # digest renders from plain tuples, with no session behind them.
            report.new_jobs = [
                (j.company, j.title, j.ats_match_score or 0.0, j.application_url,
                 age_label(j), _facts(j))
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


def render_digest(report: DailyReport, cfg, top: int = 10) -> str:
    """The digest, as plain text. This is what the user actually reads."""
    threshold = float(cfg.get("resume.min_score", 0.6))
    # Kept in UTC; the digest is read in local time (it said 11:02 for a 6:02 start).
    started = report.started_at if report.started_at.tzinfo else report.started_at.replace(tzinfo=timezone.utc)
    when = started.astimezone().strftime("%a %d %b %Y, %H:%M")
    lines = [f"JOB DIGEST — {when}", "=" * 58, ""]

    # Lead with what changed. Totals look identical every morning; changes do not.
    if report.new_jobs:
        strong = [j for j in report.new_jobs if j[2] >= threshold]
        lines.append(f"{len(report.new_jobs)} NEW since yesterday"
                     + (f" — {len(strong)} worth a look" if strong else ""))
        lines.append("")
        for company, title, score, url, age, facts in report.new_jobs[:top]:
            mark = "*" if score >= threshold else " "
            lines.append(f"  {score:>4.0%}{mark} {age:>8}  {company[:18]:<20} {title[:38]}")
            if facts:
                lines.append(f"        {facts}")
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
        f"  {report.companies} boards scraped{' by ' + report.fetched_by if report.fetched_by else ''}   "
        f"{report.jobs_seen:,} postings seen   "
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
