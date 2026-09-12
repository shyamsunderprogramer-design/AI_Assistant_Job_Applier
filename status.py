"""Live progress for long-running background work.

Discovery takes hours and the mail scan takes tens of minutes. Both are
detached, so the only way to know how they are going was to ask. This reads the
same sources they write — the database and their log files — and renders them,
so a second terminal answers the question without interrupting anything.

Read-only by construction: it opens nothing for writing and never touches the
running processes.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

BAR_WIDTH = 34


@dataclass
class Task:
    name: str
    done: int
    total: int
    running: bool
    detail: str = ""

    @property
    def pct(self) -> float:
        return 0.0 if not self.total else min(100.0, self.done * 100 / self.total)

    def bar(self, width: int = BAR_WIDTH) -> str:
        filled = int(round(self.pct / 100 * width))
        return "█" * filled + "·" * (width - filled)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _pid_of_script(name: str) -> int | None:
    """Find a running shell script; _pid_of only matches `main.py` commands."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        line = line.strip()
        # Skip the shell that is running this very lookup.
        if name in line and "ps -eo" not in line and "-c " not in line:
            try:
                return int(line.split()[0])
            except (ValueError, IndexError):
                continue
    return None


def _started_at(pid: int | None) -> float | None:
    """Wall-clock start of a process, or None if it is not running."""
    if not pid:
        return None
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    days, _, rest = out.partition("-")
    if not rest:
        days, rest = "0", days
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    seconds = int(days) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    return time.time() - seconds


def _not_yet_written(log: Path, pid: int | None) -> bool:
    """True when a running job has not written since it started.

    Its log still holds the previous run's tail, and reporting those numbers as
    current is worse than admitting the job is still warming up.
    """
    started = _started_at(pid)
    if started is None:
        return False
    try:
        return log.stat().st_mtime < started - 3
    except OSError:
        return True


# A shell invoked as `sh -c "<script>"` carries the whole script on its command
# line, so a script that merely mentions a job looks exactly like the job. The
# view is often called from such a script, and would report work as running
# minutes after it finished.
_WRAPPERS = (" -c ", "ps -eo", "pgrep", "grep ")


def _pid_of(pattern: str) -> int | None:
    """Find a running `main.py <command>` process without shelling out to pgrep."""
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,command="], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if any(w in line for w in _WRAPPERS):
            continue
        if pattern in line and "main.py" in line:
            try:
                return int(line.split(None, 1)[0])
            except (ValueError, IndexError):
                continue
    return None


def _tail_match(path: Path, pattern: re.Pattern) -> str | None:
    """First capture of the last match of `pattern` in a log's tail."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = pattern.findall(text)
    return found[-1] if found else None


def _tail_progress(path: Path, pattern: re.Pattern) -> tuple[int, int, str]:
    """Last (done, total, line) matching `pattern` in a log file."""
    if not path.exists():
        return 0, 0, ""
    try:
        # Only the tail matters, and these logs get long.
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return 0, 0, ""

    last = None
    for match in pattern.finditer(text):
        last = match
    if not last:
        return 0, 0, ""
    return int(last.group(1)), int(last.group(2)), last.group(0).strip()


MAIL_LINE = re.compile(r"(\d+)/(\d+) messages — (\d+) names, (\d+) boards")
# Printed only once the scan has written its output file and finished.
MAIL_DONE = re.compile(r"Wrote (\d+) names to")
DISCOVERY_LINE = re.compile(r"(\d+)/(\d+) names")
# Printed only on a completed sweep of the whole name list.
DISCOVERY_DONE = re.compile(r"(\d+) found from \d+ names")



def _enrichment_counts() -> tuple[int, int, int]:
    """(companies probed, still unprobed, websites found) in the company DB.

    Separate database from the job pipeline, and read-only here: this view must
    never take a write lock the enricher is waiting on.
    """
    import sqlite3
    from companies.db import DB_PATH

    if not DB_PATH.exists():
        return 0, 0, 0
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0, 0, 0
    try:
        probed = db.execute("SELECT COUNT(*) FROM website_probe").fetchone()[0]
        sites = db.execute(
            "SELECT COUNT(*) FROM companies WHERE website IS NOT NULL"
        ).fetchone()[0]
        outstanding = db.execute(
            """SELECT COUNT(*) FROM companies
               WHERE website IS NULL AND tier = 'sponsor' AND NOT EXISTS (
                   SELECT 1 FROM website_probe p WHERE p.company_id = companies.id
               )"""
        ).fetchone()[0]
        return probed, outstanding, sites
    except sqlite3.Error:
        return 0, 0, 0            # mid-migration or locked: report nothing, not a crash
    finally:
        db.close()


def collect(cfg) -> list[Task]:
    """Current state of every long-running job, plus the pipeline totals."""
    from db.models import Company, Job, ProbeLog
    from db.session import get_session
    from config.loader import PROJECT_ROOT

    with get_session() as session:
        probes = session.query(ProbeLog).count()
        boards = session.query(Company).count()
        jobs = session.query(Job).filter(Job.is_open.is_(True)).count()
        scored = (
            session.query(Job)
            .filter(Job.is_open.is_(True), Job.ats_match_score.isnot(None))
            .count()
        )

    tasks: list[Task] = []

    # -- discovery ----------------------------------------------------------
    # Count names, not probes: a name costs one probe per source, minus every
    # one the cache already answers, so a probe estimate runs far ahead of the
    # truth and a finished run looks like it stopped short.
    discovery_log = PROJECT_ROOT / "data" / "discovery_run.log"
    discovery_pid = _pid_of("discover")
    done, total, _ = _tail_progress(discovery_log, DISCOVERY_LINE)
    detail = f"{boards} boards found"
    if _not_yet_written(discovery_log, discovery_pid):
        done, detail = 0, "starting"
    elif not discovery_pid:
        finished = _tail_match(discovery_log, DISCOVERY_DONE)
        if finished:
            done = total
            detail = f"{finished} boards found"
    tasks.append(
        Task(
            name="Company discovery",
            done=done,
            total=total,
            running=_alive(discovery_pid),
            detail=detail,
        )
    )

    # -- mail scan ----------------------------------------------------------
    mail_log = PROJECT_ROOT / "data" / "mailscan.log"
    mail_pid = _pid_of("scan-mail")
    done, total, line = _tail_progress(mail_log, MAIL_LINE)
    found = re.search(r"(\d+) names", line)
    detail = f"{found.group(1)} names" if found else "searching the server"
    if _not_yet_written(mail_log, mail_pid):
        # Numbers in the log belong to a run that has already died.
        done, detail = 0, "searching the server"
    elif not mail_pid:
        # Progress lines land on multiples of 25, so a finished scan stops a
        # few short of the total and would otherwise read as "died at 99%".
        written = _tail_match(mail_log, MAIL_DONE)
        if written:
            done = total
            detail = f"{written} names written"
    tasks.append(
        Task(
            name="Mailbox scan",
            done=done,
            total=total,
            running=_alive(mail_pid),
            detail=detail,
        )
    )

    # -- website / careers enrichment ---------------------------------------
    # Rounds are a poor measure: each is a fixed 500 companies out of a few
    # hundred thousand, so "round 2 of 60" reads as 3% of a job that has barely
    # started. Count companies actually asked instead.
    enrich_pid = _pid_of_script("enrich_loop.sh")
    probed, outstanding, sites = _enrichment_counts()
    tasks.append(
        Task(
            name="Website enrichment",
            done=probed,
            total=probed + outstanding,
            running=_alive(enrich_pid),
            detail=f"{sites:,} websites found",
        )
    )

    # -- the queued daily run ------------------------------------------------
    # A chained run is real pending work: it is waiting on purpose, and a view
    # that showed nothing would look like nobody had arranged anything.
    chained = _pid_of_script("after_discovery.sh")
    daily_running = _pid_of("main.py daily")
    daily_lock = (PROJECT_ROOT / "data" / "daily.lock").exists()
    if chained or daily_running or daily_lock:
        if daily_running or daily_lock:
            detail = "running now — scrape, score, export"
        else:
            detail = "queued, waiting for discovery to finish"
        tasks.append(
            Task(
                name="Daily pipeline",
                done=0,
                total=0,          # a state, not a quantity: no bar to fill
                running=True,
                detail=detail,
            )
        )

    # -- the pipeline itself ------------------------------------------------
    tasks.append(
        Task(
            name="Jobs ranked",
            done=scored,
            total=jobs,
            running=False,
            detail=f"{jobs} open roles",
        )
    )
    return tasks


def render(tasks: list[Task]) -> str:
    lines = []
    width = max(len(t.name) for t in tasks)
    for task in tasks:
        if task.running:
            state = "running"
        elif task.total and task.done >= task.total:
            state = "done"
        elif task.done:
            state = "stopped"
        else:
            state = "idle"

        # A task with no total is a state, not a quantity: an empty bar and a
        # bare "0.0%  0" reads as a job that has done nothing.
        if task.total:
            counts = f"{task.done:,}/{task.total:,}"
            bar, pct = task.bar(), f"{task.pct:>5.1f}%"
        else:
            counts = ""
            bar, pct = " " * BAR_WIDTH, " " * 6
        lines.append(
            f"  {task.name:<{width}}  {bar}  {pct}  "
            f"{counts:>15}  {state:<8} {task.detail}"
        )
    return "\n".join(lines)


def watch(cfg, interval: float = 5.0) -> int:
    """Redraw until every task stops. Ctrl-C to leave; nothing is affected."""
    try:
        while True:
            tasks = collect(cfg)
            stamp = time.strftime("%H:%M:%S")
            print("\033[2J\033[H", end="")  # clear, home
            print(f"  JOB APPLIER — live status          {stamp}")
            print(f"  {'─' * 92}")
            print(render(tasks))
            print(f"  {'─' * 92}")

            if not any(t.running for t in tasks):
                print("  Nothing running. Ctrl-C to exit.")
            else:
                print(f"  Refreshing every {interval:.0f}s · Ctrl-C to exit "
                      f"(background jobs keep running)")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  Left the watch. Background jobs are untouched.")
        return 0
