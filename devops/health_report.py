"""Weekly health report: is the machine still doing its job?

Everything here runs unattended, so the failure to fear is the quiet one -- a
morning run that never started, a board that has failed for a week, a backup
that stopped. This reads the last seven days and says, first, whether anything
needs the person, then the numbers behind it.

    python devops/health_report.py [--days 7] [--out FILE]

Writes data/reports/health-<date>.html (the web app shows the latest at
/health) and prints one line of JSON with the problems found.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "data" / "reports"
sys.path.insert(0, str(ROOT))
from devops.backup_db import DEST as BACKUPS  # noqa: E402

FAIL_RATE_WARN = 0.05      # more than 5% of board scrapes failing in a day
BACKUP_MAX_HOURS = 36
DISK_MIN_GB = 20
LONG_RUN_HOURS = 6         # the lock is broken after 6h, so a longer run can overlap the next


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{root / 'data' / 'jobs.db'}?mode=ro", uri=True)


# "2026-10-08 06:03:47 -0500 Sleep  Entering Sleep state due to 'Maintenance Sleep':... Using Batt (Charge:4%) 941 secs"
_SLEEP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [+-]\d{4} Sleep\s+Entering Sleep state due to '([^']+)'"
                    r".*?Using (AC|Batt)(?: \(Charge:\s*(\d+)%\))?\s+(\d+) secs")


def sleep_log() -> str:
    """macOS's own record of when it slept (`pmset -g log`), or "" where there is none."""
    import subprocess
    try:
        return subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def slept_during(start: datetime, end: datetime, log_text: str) -> dict:
    """How long the Mac slept between start and end, and why: what a slow run usually is.

    A run of 12 hours on 8 Oct 2026 was the Mac asleep on battery at 4%, waking for a
    few seconds every quarter hour; the report said only "took 12 hours".
    """
    seconds, battery, lid, low = 0, False, False, None
    for line in log_text.splitlines():
        m = _SLEEP.match(line)
        if not m:
            continue
        at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        if not (start <= at <= end):
            continue
        seconds += int(m.group(5))
        battery |= m.group(3) == "Batt"
        lid |= "Clamshell" in m.group(2)
        if m.group(4):
            low = min(low, int(m.group(4))) if low is not None else int(m.group(4))
    return {"hours": round(seconds / 3600, 1), "battery": battery, "lid": lid, "charge": low}


def morning_runs(root: Path, since: date) -> dict[str, dict | None]:
    """Each day's daily run from the pipeline log, by the day it STARTED.

    {day: {"exit": int | None, "hours": float, "failed": [stage, ...]}}, or None when no
    run started that day. A run that is still going has exit None.
    """
    days: dict[str, dict | None] = {(since + timedelta(d)).isoformat(): None
                                    for d in range((date.today() - since).days + 1)}
    log = root / "data" / "daily_run.log"
    if not log.exists():
        return days
    run = None
    for line in log.read_text(errors="replace").splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2} [\d:]+)\s+=== daily (starting|finished, exit (\d+))", line)
        if m:
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            if m.group(2) == "starting":
                run = {"start": at, "exit": None, "hours": 0.0, "failed": []}
                if at.date().isoformat() in days:
                    days[at.date().isoformat()] = run
            elif run:
                run["exit"] = int(m.group(3))
                run["hours"] = round((at - run["start"]).total_seconds() / 3600, 1)
                run["end"] = at
                run = None
            continue
        # The digest names a failed stage as "  adzuna: ReadTimeout: ...".
        stage = re.match(r"  ([a-z][\w-]*): ([A-Z]\w*(?:Error|Timeout|Exception|Limited))\b", line)
        if run and stage:
            run["failed"].append(f"{stage.group(1)} ({stage.group(2)})")
    sleeping = None
    for r in days.values():
        if r:
            if r["hours"] > LONG_RUN_HOURS and r.get("end"):
                sleeping = sleep_log() if sleeping is None else sleeping
                r["slept"] = slept_during(r["start"], r["end"], sleeping)
            # The mornings this run was still going on: no new run starts while one holds the lock.
            # A run with no end that a later run followed simply died; it held nothing.
            later = [x for x in days.values() if x and x.get("start") and x["start"] > r["start"]]
            end = r.get("end") or (None if later else datetime.now())
            r["held"] = ([d for d in days if r["start"].date().isoformat() < d <= end.date().isoformat()]
                         if end else [])
            r.pop("start", None)
            r.pop("end", None)
    return days


def collect(root: Path = ROOT, days: int = 7, backups: Path = BACKUPS) -> dict:
    since = date.today() - timedelta(days=days - 1)
    s = since.isoformat()
    with _db(root) as db:
        q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
        scrapes = q("SELECT date(created_at), count(*), sum(ok), sum(jobs_new) FROM scrape_log "
                    "WHERE date(created_at) >= ? GROUP BY 1 ORDER BY 1", s)
        failing = q("SELECT source, company_slug, count(*), max(error_type) FROM scrape_log "
                    "WHERE date(created_at) >= ? AND ok = 0 GROUP BY 1, 2 HAVING count(*) >= 3 "
                    "ORDER BY 3 DESC LIMIT 15", s)
        found = q("SELECT date(found_at), count(*) FROM jobs WHERE date(found_at) >= ? GROUP BY 1 ORDER BY 1", s)
        (open_now,), = q("SELECT count(*) FROM jobs WHERE is_open = 1")
        (closed,), = q("SELECT count(*) FROM jobs WHERE date(closed_at) >= ?", s)
        (applied,), = q("SELECT count(*) FROM jobs WHERE date(applied_at) >= ?", s)
        statuses = dict(q("SELECT status, count(*) FROM jobs WHERE status NOT IN "
                          "('Not Applied', 'Manual Review', 'Closed', '') GROUP BY 1"))
    try:
        prepared = len(json.loads((root / "data" / "apply" / "prepared.json").read_text()))  # keyed by job id
    except (OSError, ValueError, TypeError):
        prepared = None

    nights = sorted(backups.glob("20*/jobs.db.gz")) if backups.exists() else []
    last_backup = None
    if nights:
        st = nights[-1].stat()
        last_backup = {"when": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes"),
                       "hours": round((datetime.now().timestamp() - st.st_mtime) / 3600, 1),
                       "mb": round(st.st_size / 1e6, 1), "count": len(nights)}
    free_gb = round(shutil.disk_usage(root).free / 1e9, 1)

    report = {"from": s, "to": date.today().isoformat(), "runs": morning_runs(root, since),
              "scrapes": [{"day": d, "boards": n, "failed": n - (ok or 0), "new": new or 0} for d, n, ok, new in scrapes],
              "failing": [{"source": a, "board": b, "failures": c, "error": e} for a, b, c, e in failing],
              "found": dict(found), "open": open_now, "closed": closed, "applied": applied,
              "statuses": statuses, "prepared": prepared, "backup": last_backup, "free_gb": free_gb}
    report["problems"] = problems(report)
    return report


def problems(r: dict) -> list[str]:
    out = []
    today = date.today().isoformat()
    for day, run in r["runs"].items():
        if run is None:
            holder = next((d for d, x in r["runs"].items() if x and day in (x.get("held") or [])), None)
            if holder:
                out.append(f"No morning run started on {day}: the run from {holder} was still going "
                           f"(a new one waits for it).")
            elif day != today:
                out.append(f"No morning run started on {day}.")
        elif run["exit"] and run["failed"]:
            out.append(f"On {day} part of the morning run failed: {', '.join(run['failed'])}; the rest ran.")
        elif run["exit"]:
            out.append(f"The morning run on {day} failed (exit {run['exit']}).")
        elif run["hours"] > LONG_RUN_HOURS:
            slept = run.get("slept") or {}
            if slept.get("hours", 0) >= 1:
                how = ("with the lid closed" if slept["lid"] else "") + (
                    (" and " if slept["lid"] else "") + "on battery"
                    + (f" (down to {slept['charge']}%)" if slept.get("charge") is not None else "")
                    if slept["battery"] else "")
                out.append(f"The morning run on {day} took {run['hours']:.0f} hours: the Mac was asleep for "
                           f"{slept['hours']:.0f} of them{(' ' + how) if how else ''}. Keep it plugged in "
                           f"with the lid open and the run takes about an hour and a half.")
            else:
                out.append(f"The morning run on {day} took {run['hours']:.0f} hours.")
    for row in r["scrapes"]:
        if row["boards"] and row["failed"] / row["boards"] > FAIL_RATE_WARN:
            out.append(f"{row['failed']} of {row['boards']} board scrapes failed on {row['day']}.")
    if r["failing"]:
        out.append(f"{len(r['failing'])} board(s) failed 3+ times this week. A board that fails five times in a "
                   f"row is switched off by itself: the company has usually moved to another job-board system.")
    if not r["backup"]:
        out.append("No database backup found.")
    elif r["backup"]["hours"] > BACKUP_MAX_HOURS:
        out.append(f"The last backup is {r['backup']['hours']:.0f} hours old.")
    if r["free_gb"] < DISK_MIN_GB:
        out.append(f"Only {r['free_gb']} GB free on the Storage drive.")
    return out


def render(r: dict) -> str:
    e = html.escape

    def table(head, rows):
        return ("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in rows)
                + "</table>") if rows else "<p class=dim>None.</p>"

    verdict = ("<p class=ok>All good — nothing needs you.</p>" if not r["problems"] else
               "<ul class=bad>" + "".join(f"<li>{e(p)}</li>" for p in r["problems"]) + "</ul>")
    b = r["backup"]
    def result(run):
        if run is None:
            return "did not start"
        if run["exit"] is None:
            return "still running"
        return "ok" if run["exit"] == 0 else ("partly failed: " + ", ".join(run["failed"]) if run["failed"] else f"exit {run['exit']}")
    runs = [(d, result(x), f"{x['hours']:.1f} h" if x and x["exit"] is not None else "") for d, x in r["runs"].items()]
    scr = [(x["day"], x["boards"], x["failed"], f"{x['failed'] / x['boards']:.1%}" if x["boards"] else "", x["new"])
           for x in r["scrapes"]]
    return f"""<!doctype html><meta charset=utf-8><title>Health {e(r['to'])}</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>table{{display:block;overflow-x:auto}}body{{font:14px -apple-system,system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#1d1d1f;background:#fff}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:22px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #e5e5ea;padding:4px 8px;text-align:left}}.ok{{color:#1a7f37;font-weight:600}}
.bad{{color:#b42318}}.dim{{color:#62626a}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}}
.grid div{{background:#f5f5f7;border-radius:8px;padding:8px}}.grid b{{display:block;font-size:18px}}
@media(prefers-color-scheme:dark){{body{{background:#1c1c1e;color:#f2f2f7}}.grid div{{background:#2c2c2e}}td,th{{border-color:#3a3a3c}}}}</style>
<h1>Weekly health · {e(r['from'])} → {e(r['to'])}</h1>
{verdict}
<div class=grid>
<div><b>{sum(r['found'].values())}</b>new jobs found</div><div><b>{r['open']}</b>open now</div>
<div><b>{r['closed']}</b>closed this week</div><div><b>{r['applied']}</b>applied this week</div>
<div><b>{'—' if r['prepared'] is None else r['prepared']}</b>ready to apply</div>
<div><b>{f"{b['hours']:.0f}h ago" if b else 'none'}</b>last backup{f" ({b['mb']} MB, {b['count']} kept)" if b else ''}</div>
<div><b>{r['free_gb']} GB</b>free on Storage</div></div>
<h2>Morning runs</h2>{table(('Day', 'Result', 'Took'), runs)}
<h2>Scraping</h2>{table(('Day', 'Boards', 'Failed', 'Rate', 'New jobs'), scr)}
<h2>Boards failing 3+ times</h2>{table(('Source', 'Board', 'Failures', 'Last error'), [tuple(x.values()) for x in r['failing']])}
<h2>Your applications</h2>{table(('Status', 'Jobs'), sorted(r['statuses'].items()))}
<p class=dim>Made {e(datetime.now().strftime('%Y-%m-%d %H:%M'))} by devops/health_report.py ·
<a href="/health?fresh=1">make a fresh one</a> · <a href="/">back to jobs</a></p>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    r = collect(days=args.days)
    out = args.out or REPORTS / f"health-{r['to']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(r))
    # The figures as data too: the web app draws them in its own pages (/health).
    out.with_suffix(".json").write_text(json.dumps(r, indent=1, default=str))
    print(json.dumps({"ok": not r["problems"], "task": "health", "report": str(out), "problems": r["problems"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
