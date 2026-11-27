"""The run's outcome, from what its steps actually did: COMPLETE, PARTIAL or FAILED (Nov 2026).

Until 26 Nov 2026 the join ended with one step, "Fail unless this is a complete refresh", that exited 1 whenever the
merge did not say complete=true. That one condition covered two different things: a run that read most boards, merged,
verified, locked and published a usable result (PARTIAL -- e.g. run #109: Workday, Workable and Jobvite deferred by
their sites' limits, 84.0% read), and a run whose merge stopped and published nothing. Both said "Not a complete
refresh", so a usable partial result looked like a technical failure, and a real failure was described as mere
incompleteness. Now each is named, from the steps' own outcomes and the coverage report:

  COMPLETE  every board read in full; merged, verified, locked and published.               Exit 0.
  PARTIAL   some boards not read (refused, rate-limited, deferred, failed at the board) -- every board still
            accounted for; merged, verified (devops/discovery_result.py build refuses to publish what the Mac would
            refuse), locked and published. Warnings say what was not read; those boards stay for recovery.
            Exit 0: incomplete coverage alone is not a technical failure. Never called complete.
  FAILED    the merge stopped; the coverage report is missing or does not account for every planned board; no
            board was read at all; a partition's machine delivered no usable result; or locking, building/verifying
            or publishing failed. Exit 1. (A failed partition's verified result of the OTHER partitions is still
            published and safe for the Mac; the run is failed all the same, because a machine of it failed.)

Counts only: this writes to the public job summary.

    python -m devops.discovery_outcome --coverage coverage.json --merge-code 3 --lock success --build success \
        --upload success --complete-upload skipped --result discovery-result.tar.enc --slot "..." --trigger schedule
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMPLETE, PARTIAL, FAILED = "COMPLETE", "PARTIAL", "FAILED"
DEADLINE = timedelta(hours=2)            # results due two hours after the window starts (3:00, 13:00 Central)


def decide(coverage: dict | None, merge_code: int | None, steps: dict) -> tuple[str, list[str], list[str]]:
    """(status, why it FAILED, what a PARTIAL run left unread). `steps`: build, upload ->
    the GitHub step outcome (success, failure, cancelled, skipped, or '' when it did not run)."""
    failed: list[str] = []
    if merge_code not in (0, 3):
        failed.append(f"the merge stopped (exit {merge_code}): nothing was merged or published"
                      if merge_code is not None else "the merge did not run")
    if not coverage:
        failed.append("there is no coverage report")
        return FAILED, failed, []
    t = coverage.get("totals") or {}
    planned = int(t.get("boards_planned") or 0)
    if not planned:
        failed.append("the plan held no boards")
    if not t.get("reconciles", False):
        failed.append("the coverage report does not account for every planned board")
    if planned and int(t.get("boards_scanned_in_full") or 0) + int(t.get("boards_read_in_part") or 0) == 0:
        failed.append("no board was read at all: nothing usable")
    lost = [p for p in coverage.get("partitions") or [] if p.get("reason") and p.get("status") == "FAILED"]
    if lost:
        failed.append(f"{len(lost)} partition(s) delivered no usable result (their boards stay for recovery)")
    complete = merge_code == 0
    if complete != (coverage.get("status") == COMPLETE):
        failed.append("the merge's exit and its coverage report disagree")
    # The verified result is what is published, complete or not (27 Nov 2026: the separate complete-refresh copy,
    # and its own lock step, are no longer made -- the result already holds that database, signed and locked).
    for step, what in (("build", "building and verifying the result"), ("upload", "publishing the verified result")):
        if merge_code in (0, 3) and steps.get(step) != "success":
            failed.append(f"{what} did not succeed ({steps.get(step) or 'did not run'})")
    if failed:
        return FAILED, failed, []
    if complete:
        return COMPLETE, [], []
    unread = []
    for site, p in sorted((coverage.get("providers") or {}).items()):
        c = p.get("counts") or {}
        missing = int(p.get("boards") or 0) - int(c.get("complete") or 0)
        if missing:
            own = int(p.get("owner_restricted") or 0)
            unread.append(f"{site}: {c.get('complete', 0):,} of {p.get('boards', 0):,} boards read in full "
                          f"({c.get('partial', 0)} in part, {int(c.get('failed') or 0) - own} failed, "
                          f"{own} owner-restricted, {c.get('rate_limited', 0)} refused/rate-limited, "
                          f"{p.get('deferred', 0)} deferred)")
    return PARTIAL, [], unread


def _minutes(a: str | None, b: str | None) -> float | None:
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((tb - ta).total_seconds() / 60, 1)


def summary(status: str, failed: list, unread: list, coverage: dict | None, *, slot: str = "", trigger: str = "",
            started: str = "", result_bytes: int | None = None, steps: dict | None = None,
            now: datetime | None = None, window_start: datetime | None = None) -> str:
    """The job summary (Markdown, counts only)."""
    now = now or datetime.now(timezone.utc)
    steps = steps or {}
    t = (coverage or {}).get("totals") or {}
    head = {COMPLETE: "COMPLETE — verified result published, every board read in full",
            PARTIAL: "PARTIAL — verified usable result published; coverage incomplete (not a complete refresh)",
            FAILED: "FAILED — see the reasons below"}[status]
    lines = [f"## Discovery outcome: {head}", ""]
    if (coverage or {}).get("restored_from"):
        lines += [f"> **Saved database restored** from run {coverage['restored_from']}'s verified result (the cache "
                  "held none): history continues from that run.", ""]
    if (coverage or {}).get("cold_start"):
        lines += ["> **Cold start:** the saved database was missing. Board history, each site's last refusal and "
                  "earlier inventories start from this run.", ""]
    if failed:
        lines += [f"- **Failed:** {w}" for w in failed] + [""]
    rows = [("Discovery window", slot or "—"), ("Started by", trigger or "—")]
    if t:
        new = sum(int((p.get("merged") or {}).get("added") or 0) for p in (coverage or {}).get("partitions") or [])
        feeds = (coverage or {}).get("feeds") or {}
        rows += [("Boards planned", f"{t.get('boards_planned', 0):,}"),
                 ("Read in full", f"{t.get('boards_scanned_in_full', 0):,}"),
                 ("Read in part", f"{t.get('boards_read_in_part', 0):,}"),
                 ("Failed", f"{t.get('boards_failed', 0):,}"),
                 ("Owner-restricted (robots.txt)", f"{t.get('boards_owner_restricted', 0):,}"),
                 ("Refused / rate-limited", f"{t.get('boards_rate_limited', 0):,}"),
                 ("Deferred (site quota / cooling off / no internet)",
                  f"{t.get('boards_deferred_quota', 0):,} / {t.get('boards_deferred_cooling', 0):,} / "
                  f"{t.get('boards_deferred_network', 0):,}"),
                 ("Out of time / not attempted", f"{t.get('boards_timed_out', 0):,} / {t.get('boards_not_attempted', 0):,}"),
                 ("Coverage (of the active boards planned)", f"{t.get('coverage_pct', 0)}%"),
                 ("Coverage (of every known board, retired included)",
                  f"{t['coverage_of_known_pct']}% of {t.get('boards_known', 0):,}" if "coverage_of_known_pct" in t
                  else "—"),
                 ("New postings (boards / job-search sites)", f"{new:,} / {int(feeds.get('added') or 0):,}"),
                 ("Merge and checks", f"{t.get('merge_seconds', '—')} s"),
                 ("Still to read (cloud recovery)", f"{t.get('boards_needing_recovery', 0):,} boards")]
    if result_bytes is not None:
        rows.append(("Verified result (encrypted)", f"{result_bytes / 1e6:,.1f} MB"))
    rows += [("Built, verified and encrypted", steps.get("build") or "did not run"),
             ("Published", steps.get("upload") or "did not run")]
    total = _minutes(started, now.isoformat()) if started else None
    if total is not None:
        rows.append(("Total so far", f"{total} min"))
    if window_start is not None:
        due = window_start + DEADLINE
        rows.append(("Deadline", f"{'met' if status != FAILED and now <= due else 'MISSED'} "
                                 f"(due {due:%H:%M} UTC)"))
    lines += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows] + [""]
    sites: dict = {}
    for p in (coverage or {}).get("partitions") or []:
        e = sites.setdefault(p.get("provider") or "?", {"parts": 0, "minutes": 0.0, "requests": 0, "refused": 0,
                                                         "deferred": 0, "recovered": 0, "waited_s": 0.0})
        r = p.get("retry") or {}
        e["parts"] += 1
        e["minutes"] = max(e["minutes"], float(p.get("minutes") or 0))
        e["requests"] += int(p.get("requests") or 0)
        e["refused"] += int(p.get("rate_limited_requests") or 0)
        for k in ("deferred", "recovered"):
            e[k] += int(r.get(k) or 0)
        e["waited_s"] = max(e["waited_s"], float(r.get("waited_s") or 0))
    if sites:
        lines += ["| Site | Parts | Slowest (min) | Requests | Per second, all readers | Refused (429) | "
                  "Deferred / recovered on retry | Longest cool-off wait |", "|---|---|---|---|---|---|---|---|"]
        for site, e in sorted(sites.items()):
            rate = e["requests"] / (e["minutes"] * 60) if e["minutes"] else 0
            lines.append(f"| {site} | {e['parts']} | {e['minutes']:.1f} | {e['requests']:,} | {rate:.2f} | "
                         f"{e['refused']:,} | {e['deferred']:,} / {e['recovered']:,} | {e['waited_s'] / 60:.1f} min |")
        lines.append("")
    inv = ((coverage or {}).get("inventory") or {}).get("sites") or {}
    if inv:
        provs = (coverage or {}).get("providers") or {}
        lines += ["| Site | Known boards | Active | Deactivated | Owner-restricted | Asked this run | Read in full | "
                  "Refused | Deferred | Read in last 7 days | Read in last 30 days |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for site, v in sorted(inv.items()):
            p = provs.get(site) or {}
            c = p.get("counts") or {}
            lines.append(f"| {site} | {v['known']:,} | {v['active']:,} | {v['deactivated']:,} | "
                         f"{v['owner_restricted']:,} | {int(p.get('attempted') or 0):,} | "
                         f"{int(c.get('complete') or 0):,} | {int(c.get('rate_limited') or 0):,} | "
                         f"{int(p.get('deferred') or 0):,} | {v['read_7d']:,} | {v['read_30d']:,} |")
        lines.append("")
    cool = {k: v for k, v in ((coverage or {}).get("provider_state") or {}).items() if v.get("cool_until")}
    if cool:
        lines += [f"- **{site} cooling off** until {v['cool_until']} UTC ({v['reason']}): the next runs ask none of "
                  "its boards before then." for site, v in sorted(cool.items())] + [""]
    if (coverage or {}).get("reactivated"):
        lines += ["- **Active again** (retired on a refusal, the host's own error or robots.txt, never on what the "
                  "host said about the board): " + ", ".join(f"{s} {n}" for s, n in
                                                            sorted(coverage["reactivated"].items())), ""]
    parts = [(p.get("id"), p.get("status"), p.get("minutes")) for p in (coverage or {}).get("partitions") or []]
    if parts:
        lines += ["| Partition | Status | Minutes |", "|---|---|---|"]
        lines += [f"| {pid} | {st} | {m if m is not None else '—'} |" for pid, st, m in parts] + [""]
    if unread:
        lines += ["**Not read in full this run** (their boards stay for the next runs):", ""]
        lines += [f"- {u}" for u in unread] + [""]
    return "\n".join(lines)


def _window_start(slot: str) -> datetime | None:
    """'2026-11-26 11:00 CST' -> that moment in UTC."""
    if not slot:
        return None
    from zoneinfo import ZoneInfo
    try:
        local = datetime.strptime(" ".join(slot.split()[:2]), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Chicago"))
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--coverage", type=Path, required=True)
    ap.add_argument("--merge-code", default="")
    for step in ("lock", "build", "upload", "complete-upload"):
        ap.add_argument(f"--{step}", default="")
    ap.add_argument("--result", type=Path, default=None)
    ap.add_argument("--slot", default="")
    ap.add_argument("--trigger", default="")
    ap.add_argument("--started", default="")
    args = ap.parse_args(argv)
    coverage = None
    if args.coverage.is_file():
        try:
            coverage = json.loads(args.coverage.read_text())
        except ValueError:
            coverage = None
    code = int(args.merge_code) if str(args.merge_code).strip().lstrip("-").isdigit() else None
    steps = {"build": args.build, "upload": args.upload}     # --lock / --complete-upload: accepted, no longer used
    status, failed, unread = decide(coverage, code, steps)
    size = args.result.stat().st_size if args.result and args.result.is_file() else None
    text = summary(status, failed, unread, coverage, slot=args.slot, trigger=args.trigger, started=args.started,
                   result_bytes=size, steps=steps, window_start=_window_start(args.slot))
    page = os.environ.get("GITHUB_STEP_SUMMARY")
    if page:
        with open(page, "a") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"status={status}\n")
    # Annotations: on the run's page, beside the summary.
    for why in failed:
        print(f"::error::Discovery FAILED: {why}")
    if (coverage or {}).get("cold_start"):
        print("::warning::Cold start: the saved database was missing; history starts from this run.")
    if status == PARTIAL:
        t = (coverage or {}).get("totals") or {}
        print(f"::warning::Discovery PARTIAL (not a complete refresh): {t.get('boards_scanned_in_full', 0):,} of "
              f"{t.get('boards_planned', 0):,} boards read in full ({t.get('coverage_pct', 0)}%); the verified result "
              f"is published; {t.get('boards_needing_recovery', 0):,} boards stay for the next runs.")
        for u in unread:
            print(f"::warning::Not read in full: {u}")
    return 1 if status == FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
