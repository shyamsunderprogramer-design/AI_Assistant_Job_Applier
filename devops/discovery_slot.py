"""Which scheduled window a cloud run belongs to, and whether that window was read already (Nov 2026).

The discovery runs twice a day, started from the cloud: 1:00 and 11:00 America/Chicago (daylight saving
followed by the time zone, never by a fixed UTC offset), its results published by 3:00 and 13:00. Two
starters may ask for the same window: the outside scheduler at the minute (cron-job.org, calling
workflow_dispatch with trigger=scheduler), and GitHub's own schedule as a fallback, which started 3-12 hours
late on every one of 68 measured days. The first of them to run reads the window; any later one for the same
window stops before reading anything. A run started with the button (trigger=manual) always runs.

A window counts as read when an earlier run created inside it got as far as its partitions (a `scrape ...` job
that was not skipped). The workflow's concurrency group runs one at a time, so by the time a later run asks,
the earlier one has finished or never started.

    python -m devops.discovery_slot --event schedule --trigger "" --run-id 123 --repo owner/name
prints `go=true|false`, `slot=...` and `why=...` lines for $GITHUB_OUTPUT. Standard library and the gh CLI only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
STARTS = ((1, 0), (11, 0))          # local start times of the two windows
EARLY = timedelta(minutes=10)       # a start a few minutes early still belongs to the window it is for
WORKFLOW = "daily-scrape.yml"


def slot_start(now: datetime) -> datetime:
    """The start (UTC) of the window `now` belongs to: the latest local start at or before now + EARLY."""
    local = (now + EARLY).astimezone(TZ)
    best = None
    for days in (0, 1):
        day = (local - timedelta(days=days)).date()
        for hour, minute in STARTS:
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
            if start <= local and (best is None or start > best):
                best = start
    assert best is not None                 # yesterday always holds both starts
    return best.astimezone(timezone.utc)


def label(start: datetime) -> str:
    local = start.astimezone(TZ)
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()}"


def decide(event: str, trigger: str, now: datetime, others: list[dict]) -> dict:
    """Whether this run reads. `others`: the workflow's other runs, each {created: datetime, scraped: bool}."""
    if event == "workflow_dispatch" and (trigger or "manual") != "scheduler":
        return {"go": True, "slot": "", "why": "started by hand: it always runs"}
    start = slot_start(now)
    for run in others:
        if run["created"] >= start - EARLY and run["scraped"]:
            return {"go": False, "slot": label(start),
                    "why": f"the {label(start)} window was read already (run {run.get('id', '?')})"}
    return {"go": True, "slot": label(start), "why": f"the first run of the {label(start)} window"}


def _gh(path: str) -> dict:
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"gh api {path.split('?')[0]}: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)


def others_in(repo: str, since: datetime, run_id: str) -> list[dict]:
    """The workflow's other runs created since `since`, and whether each reached its partitions."""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = _gh(f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?created=%3E%3D{stamp}&per_page=50")
    found = []
    for r in runs.get("workflow_runs") or []:
        if str(r["id"]) == str(run_id) or r.get("conclusion") == "cancelled":
            continue
        jobs = _gh(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100").get("jobs") or []
        scraped = any(j["name"].startswith("scrape ") and j.get("conclusion") not in ("skipped", "cancelled")
                      for j in jobs)
        found.append({"id": r["id"], "scraped": scraped,
                      "created": datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))})
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--event", required=True)
    ap.add_argument("--trigger", default="")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    others = []
    if not (args.event == "workflow_dispatch" and (args.trigger or "manual") != "scheduler"):
        others = others_in(args.repo, slot_start(now) - EARLY, args.run_id)
    d = decide(args.event, args.trigger, now, others)
    print(f"go={'true' if d['go'] else 'false'}")
    print(f"slot={d['slot']}")
    print(f"why={d['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
