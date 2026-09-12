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
        if pattern in line and "main.py" in line and "ps -eo" not in line:
            try:
                return int(line.split(None, 1)[0])
            except (ValueError, IndexError):
                continue
    return None


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

# enrich_loop.sh runs this many rounds before it stops on its own.
ENRICH_ROUNDS = 60


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
    names_file = PROJECT_ROOT / "data" / "discovery_names.txt"
    planned = 0
    if names_file.exists():
        planned = sum(
            1 for line in names_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.startswith("#")
        )
    # One probe per source per name, capped at --max-slugs 1 in the live run.
    expected_probes = planned * 3
    tasks.append(
        Task(
            name="Company discovery",
            done=probes,
            total=expected_probes,
            running=_alive(_pid_of("discover")),
            detail=f"{boards} boards found",
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
    enrich_log = PROJECT_ROOT / "data" / "companies_build" / "enrich_loop.log"
    enrich_pid = _pid_of_script("enrich_loop.sh")
    rounds = 0
    if enrich_log.exists():
        found_rounds = re.findall(
            r"round (\d+)", enrich_log.read_text(encoding="utf-8", errors="replace")
        )
        rounds = int(found_rounds[-1]) if found_rounds else 0
    tasks.append(
        Task(
            name="Website enrichment",
            done=rounds,
            total=ENRICH_ROUNDS,
            running=_alive(enrich_pid),
            detail=f"round {rounds} of {ENRICH_ROUNDS}" if rounds else "not started",
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

        counts = f"{task.done:,}/{task.total:,}" if task.total else f"{task.done:,}"
        lines.append(
            f"  {task.name:<{width}}  {task.bar()}  {task.pct:>5.1f}%  "
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
