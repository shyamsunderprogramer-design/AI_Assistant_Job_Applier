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
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "data" / "reports"
sys.path.insert(0, str(ROOT))
from devops.backup_db import destination as backup_folder  # noqa: E402

FAIL_RATE_WARN = 0.05      # more than 5% of board scrapes failing in a day
BACKUP_MAX_HOURS = 36
DISK_MIN_GB = 20
# Awake, a morning run took about six hours once the board list passed 20,000 (Oct 2026);
# a run held its lock however long it went, so a longer one only means a slow morning.
LONG_RUN_HOURS = 8
FAILING_LISTED = 15        # boards named in the table; the count covers every one


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
    seconds, battery, lid, low, high = 0, False, False, None, None
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
            high = max(high, int(m.group(4))) if high is not None else int(m.group(4))
    # The highest too: on 15 Oct every sleep was at 100%, and "down to 100%" read wrong.
    return {"hours": round(seconds / 3600, 1), "battery": battery, "lid": lid, "charge": low, "charge_max": high}


def morning_runs(root: Path, since: date) -> dict[str, dict | None]:
    """Each day's daily run from the pipeline log, by the day it STARTED.

    {day: {"exit": int | None, "hours": float, "failed": [stage, ...]}}. A run that is
    still going has exit None; one stopped from the Jobs page has "stopped", and one
    that ended with no line in the log has "unfinished". A morning with no run is None,
    or {"held_by": day} when an earlier run still held the lock -- one that began before
    these days too: on 18 Oct the report said only "No morning run started on
    2026-10-12", because the run that held it had started on the 10th.
    """
    days: dict[str, dict | None] = {(since + timedelta(d)).isoformat(): None
                                    for d in range((date.today() - since).days + 1)}
    log = root / "data" / "daily_run.log"
    if not log.exists():
        return days
    runs, run = [], None
    for line in log.read_text(errors="replace").splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2} [\d:]+)\s+=== daily (starting|finished, exit (\d+)|stopped)", line)
        if m:
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            if m.group(2) == "starting":
                run = {"start": at, "exit": None, "hours": 0.0, "failed": []}
                runs.append(run)
            elif run:
                if m.group(3):
                    run["exit"] = int(m.group(3))
                else:                       # "=== daily stopped from the Jobs page ==="
                    run["stopped"] = True
                run["hours"] = round((at - run["start"]).total_seconds() / 3600, 1)
                run["end"] = at
                run = None
            continue
        # The digest names a failed stage as "  adzuna: ReadTimeout: ...".
        stage = re.match(r"  ([a-z][\w-]*): ([A-Z]\w*(?:Error|Timeout|Exception|Limited))\b", line)
        if run and stage:
            run["failed"].append(f"{stage.group(1)} ({stage.group(2)})")
    sleeping = None
    for i, r in enumerate(runs):
        day = r["start"].date().isoformat()
        # The mornings this run was still going on: no new run starts while one holds the lock.
        # A run with no end that a later run followed simply died; it held nothing.
        end = r.get("end") or (None if i + 1 < len(runs) else datetime.now())
        r["held"] = [d for d in days if end and day < d <= end.date().isoformat()]
        for d in r["held"]:
            if days[d] is None:
                days[d] = {"held_by": day}
        if day not in days:
            continue
        if end is None:
            r["unfinished"] = True
        if r["hours"] > LONG_RUN_HOURS and r.get("end"):
            sleeping = sleep_log() if sleeping is None else sleeping
            r["slept"] = slept_during(r["start"], r["end"], sleeping)
        r.pop("start")
        r.pop("end", None)
        days[day] = r
    return days


def collect(root: Path = ROOT, days: int = 7, backups: Path | None = None) -> dict:
    backups = backups or backup_folder(root)
    since = date.today() - timedelta(days=days - 1)
    s = since.isoformat()
    with _db(root) as db:
        q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
        # The Mac's own days, as the morning runs are counted: by UTC, a run that went on
        # past 7 PM was split across two days.
        day = "date(created_at, 'localtime')"
        scrapes = q(f"SELECT {day}, count(*), sum(ok), sum(jobs_new) FROM scrape_log "
                    f"WHERE {day} >= ? GROUP BY 1 ORDER BY 1", s)
        failed_by_source = q(f"SELECT {day}, source, count(*) FROM scrape_log WHERE {day} >= ? AND ok = 0 "
                             "GROUP BY 1, 2 ORDER BY 3 DESC", s)
        # Every board that keeps failing, not the first 15: the line said "15 board(s)" on every
        # report, when 18 Oct had 87. A host turning us away is not the board's doing: apart below.
        failing = q(f"SELECT source, company_slug, count(*), max(error_type) FROM scrape_log "
                    f"WHERE {day} >= ? AND ok = 0 AND error_type != 'RateLimited' "
                    "GROUP BY 1, 2 HAVING count(*) >= 3 ORDER BY 3 DESC", s)
        refused = q(f"SELECT source, sum(error_type = 'RateLimited'), count(*) FROM scrape_log "
                    f"WHERE {day} >= ? GROUP BY 1 HAVING sum(error_type = 'RateLimited') > 0 ORDER BY 2 DESC", s)
        (boards,), = q("SELECT count(*) FROM companies WHERE active = 1")
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

    report = {"from": s, "to": date.today().isoformat(), "made": datetime.now().isoformat(timespec="minutes"),
              "runs": morning_runs(root, since),
              "scrapes": [{"day": d, "boards": n, "failed": n - (ok or 0), "new": new or 0,
                           "sources": {src: f for dd, src, f in failed_by_source if dd == d}}
                          for d, n, ok, new in scrapes],
              "boards": boards,
              "failing": [{"source": a, "board": b, "failures": c, "error": e}
                          for a, b, c, e in failing[:FAILING_LISTED]],
              "failing_count": len(failing),
              "failing_sources": dict(Counter(a for a, *_ in failing).most_common()),
              "refused": [{"source": a, "refused": n, "reads": m} for a, n, m in refused],
              "found": dict(found), "open": open_now, "closed": closed, "applied": applied,
              "statuses": statuses, "prepared": prepared, "backup": last_backup, "free_gb": free_gb}
    report["problems"] = problems(report)
    return report


def problems(r: dict) -> list[str]:
    out = []
    today = date.today().isoformat()
    for day, run in r["runs"].items():
        if run is None or "held_by" in run:
            if run:
                out.append(f"No morning run started on {day}: the run from {run['held_by']} was still going "
                           f"(a new one waits for it).")
            elif day != today:
                out.append(f"No morning run started on {day}.")
        elif run.get("stopped"):
            continue                        # stopped from the Jobs page: someone meant it
        elif run.get("unfinished"):
            out.append(f"The morning run on {day} stopped partway: the run log has no end for it.")
        elif run["exit"] and run["failed"]:
            out.append(f"On {day} part of the morning run failed: {', '.join(run['failed'])}; the rest ran.")
        elif run["exit"]:
            out.append(f"The morning run on {day} failed (exit {run['exit']}).")
        elif run["hours"] > LONG_RUN_HOURS:
            slept = run.get("slept") or {}
            if slept.get("hours", 0) >= 1:
                low, high = slept.get("charge"), slept.get("charge_max")
                charge = "" if low is None else f" (at {low}%)" if low == high else f" (down to {low}%)"
                how = " and ".join((["with the lid closed"] if slept["lid"] else [])
                                   + ([f"on battery{charge}"] if slept["battery"] else []))
                out.append(f"The morning run on {day} took {run['hours']:.0f} hours: the Mac was asleep for "
                           f"{slept['hours']:.0f} of them{(' ' + how) if how else ''}. Keep it plugged in "
                           f"with the lid open so it stays awake through the run.")
            else:
                out.append(f"The morning run on {day} took {run['hours']:.0f} hours.")
    # A day that read under a third of the boards is a run still going or cut short, too few
    # to judge: made mid-run on 18 Oct, the report called 15 failures in the first 135 a problem.
    for row in r["scrapes"]:
        if row["boards"] * 3 >= r.get("boards", 0) and row["failed"] > FAIL_RATE_WARN * row["boards"]:
            worst = ", ".join(f"{s} {n}" for s, n in list((row.get("sources") or {}).items())[:3])
            out.append(f"{row['failed']} of {row['boards']} board scrapes failed on {row['day']}"
                       f"{f' ({worst})' if worst else ''}.")
    if r.get("failing_count", len(r["failing"])):
        where = ", ".join(f"{s} {n}" for s, n in list((r.get("failing_sources") or {}).items())[:3])
        out.append(f"{r.get('failing_count', len(r['failing']))} board(s) failed 3+ times this week"
                   f"{f' ({where})' if where else ''}. A board that fails five times in a row is switched off "
                   f"by itself: the company has usually moved to another job-board system.")
    for x in r.get("refused", []):
        if x["refused"] > FAIL_RATE_WARN * x["reads"]:
            out.append(f"{x['source'].capitalize()} turned us away for asking too often on {x['refused']} of "
                       f"{x['reads']} board reads this week, so those boards were not read.")
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
        if run is None or "held_by" in run:
            return "did not start" + (f": the run from {run['held_by']} was still going" if run else "")
        if run.get("stopped"):
            return "stopped from the Jobs page"
        if run.get("unfinished"):
            return "did not finish"
        if run["exit"] is None:
            return "still running"
        return "ok" if run["exit"] == 0 else ("partly failed: " + ", ".join(run["failed"]) if run["failed"] else f"exit {run['exit']}")

    def took(run):
        done = run and "held_by" not in run and (run["exit"] is not None or run.get("stopped"))
        return f"{run['hours']:.1f} h" if done else ""
    runs = [(d, result(x), took(x)) for d, x in r["runs"].items()]
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
<h2>Boards failing 3+ times ({r['failing_count']})</h2>{table(('Source', 'Board', 'Failures', 'Last error'), [tuple(x.values()) for x in r['failing']])}
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
