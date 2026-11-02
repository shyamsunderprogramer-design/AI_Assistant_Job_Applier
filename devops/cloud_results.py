"""The GitHub fetch's postings brought into this Mac's database -- or a fetch here when there is none.

GitHub Actions reads every job board, whether the laptop is open, shut or off
(.github/workflows/daily-scrape.yml, four parts at once: devops/fetch_parts.py), and leaves the
postings on the run, encrypted. The morning run here (devops/daily_pipeline.sh -> main.py daily
--cloud) then:

  * takes every finished run it has not taken yet -- minutes, where a fetch here takes hours;
  * waits for one under way, rather than fetching the same boards twice;
  * fetches on this Mac when GitHub has nothing, fails, or is out of reach.

With `cloud.trigger_runs: true` in backend/config/config.yaml (off until switched on, 2 Nov 2026)
this Mac also starts the fetches itself, so the morning list is ready in minutes:

  * at 8 pm (`evening`, a launchd job; until midnight if the Mac was asleep at 8);
  * in the morning, when the run it took is hours old: a second fetch for the postings of the
    night, waited for with no lock held (`wait_top_up`) and added under the morning lock when it
    finishes (`top_up`, run by `main.py daily --top-up`);
  * in the morning, when there is nothing to take and nothing under way: one fetch, waited for.

Without it, only GitHub's own schedule runs, and only a run started today counts, as before.

The cloud reads its boards from a list kept out of the public repository: this Mac sends
backend/config/boards.csv as an encrypted release asset whenever the list changes, and the run
unlocks it with the same passphrase. Both directions use RESULTS_PASSPHRASE from .env, which the
GitHub secret of the same name holds too.

    python -m devops.cloud_results state         # ready | running | collected | none
    python -m devops.cloud_results collect       # bring in the finished runs not taken yet
    python -m devops.cloud_results evening       # start the evening fetch (8 pm to midnight)
    python -m devops.cloud_results wait-top-up   # wait for the night's fetch: exit 0 when ready
    python -m devops.cloud_results push-boards   # send the board list if it changed
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

WORKFLOW = "Daily scrape"
ARTIFACT = "jobs-db-encrypted"
RELEASE = "cloud-boards"                       # the release whose asset is the encrypted board list
BOARDS = PROJECT_ROOT / "backend" / "config" / "boards.csv"
BOARDS_ASSET = "boards.csv.enc"
COLLECTED = PROJECT_ROOT / "data" / "cloud_collected.json"    # the last run brought in, and what it gave
BOARDS_SENT = PROJECT_ROOT / "data" / "cloud_boards_sent.txt"  # the list's fingerprint when last sent
TOPUP = PROJECT_ROOT / "data" / "cloud_topup.json"   # the night's fetch asked for, to add when done
WAIT_S = 3 * 3600                              # longest wait for a run under way, before fetching here
POLL_S = 120
FRESH_H = 20        # a run that finished longer ago than this is yesterday's news: never taken
TOPUP_AFTER_H = 3   # postings older than this when taken: ask for the night's as well
TOPUP_WAIT_S = 3 * 3600
TOPUP_STALE_H = 6   # an ask for the night's fetch older than this is dropped
APPEAR_S = 10 * 60  # how long a fetch just asked for may take to show on GitHub
EVENING = (20, 24)  # the evening fetch starts between 8 pm and midnight, local time
# Both ends name the digest: the cloud's OpenSSL 3 and this Mac's LibreSSL must derive the same key.
CIPHER = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256"]


class CloudError(RuntimeError):
    """The cloud could not be read: no gh, no passphrase, a download or decrypt that failed."""


def _tool(name: str) -> str:
    """gh and openssl where a launchd job finds them too: its PATH has no Homebrew."""
    found = shutil.which(name) or next((str(p) for p in (Path("/opt/homebrew/bin") / name,
                                                         Path("/usr/local/bin") / name,
                                                         Path("/usr/bin") / name) if p.exists()), None)
    if not found:
        raise CloudError(f"{name} is not installed")
    return found


def _passphrase() -> str:
    value = os.environ.get("RESULTS_PASSPHRASE", "")
    if not value:
        try:
            for line in (PROJECT_ROOT / ".env").read_text().splitlines():
                if line.startswith("RESULTS_PASSPHRASE="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
    if not value:
        raise CloudError("RESULTS_PASSPHRASE is not in .env")
    return value


def _gh(*args: str, timeout: int = 120) -> str:
    done = subprocess.run([_tool("gh"), *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode != 0:
        raise CloudError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: "
                         f"{(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


def _openssl(args: list[str], passphrase: str) -> None:
    done = subprocess.run([_tool("openssl"), *CIPHER, *args, "-pass", "env:CLOUD_PASS"],
                          capture_output=True, text=True, env={**os.environ, "CLOUD_PASS": passphrase},
                          timeout=300)
    if done.returncode != 0:
        raise CloudError("openssl: " + (done.stderr.strip()[:200] or "failed"))


def _local_day(stamp: str):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date()


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _hours_ago(stamp: str, now: datetime | None = None) -> float:
    return ((now or datetime.now(timezone.utc)) - _when(stamp)).total_seconds() / 3600


def recent_runs() -> list[dict]:
    """The fetch's last runs on GitHub, newest first."""
    return json.loads(_gh("run", "list", "--workflow", WORKFLOW, "--limit", "20", "--json",
                          "databaseId,status,conclusion,createdAt,updatedAt") or "[]")


def _last_taken() -> int:
    """The newest run brought in here. GitHub numbers runs in the order they start."""
    try:
        return int(_collected().get("run") or 0)
    except (TypeError, ValueError):
        return 0


def _counts(run: dict, today_only: bool, now: datetime | None = None) -> bool:
    """Whether a run is recent enough to stand for this morning's postings: started today, as
    before, when this Mac starts no fetches; finished (or started, while under way) within
    FRESH_H hours when it does, so last night's 8 pm fetch counts at 6 am."""
    if today_only:
        return _local_day(run["createdAt"]) == (now or datetime.now(timezone.utc)).astimezone().date()
    stamp = run.get("updatedAt") if run.get("status") == "completed" else run["createdAt"]
    return _hours_ago(stamp or run["createdAt"], now) <= FRESH_H


def pending(runs: list[dict], today_only: bool, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(finished and not yet taken, oldest first; under way). A failed run is neither: a fetch
    that failed is never taken, and the Mac fetches instead when nothing else is left."""
    last = _last_taken()
    recent = [r for r in runs if _counts(r, today_only, now)]
    ready = sorted((r for r in recent if r["status"] == "completed" and r.get("conclusion") == "success"
                    and r["databaseId"] > last), key=lambda r: r["databaseId"])
    going = [r for r in recent if r["status"] != "completed"]
    return ready, going


def trigger_on() -> bool:
    """Whether this Mac starts GitHub fetches itself: `cloud.trigger_runs` in config.yaml."""
    try:
        from backend.config.loader import load_config
        return load_config().get("cloud.trigger_runs", False) is True
    except Exception:
        return False


def dispatch() -> None:
    """Start the fetch on GitHub now (its own "Run workflow" button)."""
    _gh("workflow", "run", WORKFLOW)


def usual_minutes() -> int | None:
    """How long the last few successful runs took, so a wait can say how long it may be."""
    runs = json.loads(_gh("run", "list", "--workflow", WORKFLOW, "--status", "success", "--limit", "5",
                          "--json", "createdAt,updatedAt") or "[]")
    spans = [(datetime.fromisoformat(r["updatedAt"].replace("Z", "+00:00"))
              - datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))).total_seconds() / 60
             for r in runs]
    return round(sorted(spans)[len(spans) // 2]) if spans else None


def _collected() -> dict:
    try:
        return json.loads(COLLECTED.read_text())
    except (OSError, ValueError):
        return {}


def state(runs: list[dict] | None = None, today_only: bool | None = None) -> str:
    """ready (finished, not yet here), running, collected (the newest is here already), or none."""
    runs = recent_runs() if runs is None else runs
    today_only = (not trigger_on()) if today_only is None else today_only
    ready, going = pending(runs, today_only)
    if ready:
        return "ready"
    if going:
        return "running"
    taken = _last_taken()
    return "collected" if any(r["databaseId"] == taken for r in runs if _counts(r, today_only)) else "none"


def collect(run_id: int, started: str = "") -> dict:
    """Download, unlock and merge one run's postings. Returns what the merge did.
    `started`: the run's createdAt, so the boards it read are counted from then (UTC, as stored)."""
    from backend.config.loader import load_config
    from data_engineering.scraper.filters import resolve_filter
    from devops.merge_cloud import merge
    search = resolve_filter(load_config())        # what a fetch here would keep
    with tempfile.TemporaryDirectory() as tmp:
        _gh("run", "download", str(run_id), "--name", ARTIFACT, "--dir", tmp, timeout=900)
        locked, db = Path(tmp) / "jobs.db.enc", Path(tmp) / "jobs.db"
        if not locked.exists():
            raise CloudError(f"run {run_id} has no {ARTIFACT}")
        _openssl(["-d", "-in", str(locked), "-out", str(db)], _passphrase())
        try:
            with sqlite3.connect(db) as check:
                since = (datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(timezone.utc)
                         .strftime("%Y-%m-%d %H:%M:%S") if started else "")
                boards = check.execute("SELECT COUNT(*) FROM companies WHERE last_scraped_at >= ?",
                                       (since,)).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise CloudError(f"the unlocked file is not a database ({exc}); yours is untouched") from exc
        # Into the live file, in one transaction: a failure leaves nothing half-merged. The board
        # search waits meanwhile, so the two never write the database at once.
        from devops.probe_runner import paused
        with paused("the GitHub fetch's postings are brought in"):
            result = merge(db, PROJECT_ROOT / "data" / "jobs.db", search=search)
    result.update(run=max(run_id, _last_taken()), boards=boards,
                  at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    COLLECTED.parent.mkdir(parents=True, exist_ok=True)
    COLLECTED.write_text(json.dumps(result))
    return result


def _say_got(got: dict, say) -> None:
    news = "".join(f", {got[k]:,} {what}" for k, what in (("edited", "edited by the employer"),
                                                         ("typed", "given a job type by their board"))
                   if got.get(k))
    left = f" ({got['skipped']:,} more your search leaves out)" if got.get("skipped") else ""
    say(f"cloud: {got['added']:,} new postings{left}, {got['refreshed']:,} seen again{news}, "
        f"{got.get('closed', 0):,} taken down, from {got['boards']:,} boards")


def _take(ready: list[dict], say) -> dict | None:
    """Each finished run brought in, oldest first, and what they gave together. A run that cannot
    be brought in is said and left for next time; None when none could."""
    total: dict = {}
    for run in ready:
        say(f"cloud: bringing in today's GitHub fetch (run {run['databaseId']})…")
        try:
            got = collect(run["databaseId"], run["createdAt"])
        except CloudError as exc:
            say(f"cloud: run {run['databaseId']} could not be brought in ({str(exc)[:160]})")
            continue
        for key, value in got.items():
            if key == "boards":
                total[key] = max(total.get(key, 0), value)
            elif isinstance(value, int) and key != "run":
                total[key] = total.get(key, 0) + value
        total.update(run=got.get("run"), at=got.get("at"), runs=[*total.get("runs", []), run["databaseId"]])
    if not total:
        return None
    total.setdefault("boards", 0)
    for key in ("added", "refreshed"):
        total.setdefault(key, 0)
    COLLECTED.write_text(json.dumps(total))
    _say_got(total, say)
    return total


def _ask_top_up(newest: dict, going: list[dict], say) -> None:
    """The run just taken read the boards hours ago (last night's): ask for the night's postings
    too, added by `main.py daily --top-up` when that fetch finishes. A run under way already
    (GitHub's own schedule) is that fetch."""
    if _hours_ago(newest.get("updatedAt") or newest["createdAt"]) < TOPUP_AFTER_H:
        return
    try:
        if not going:
            dispatch()
        TOPUP.parent.mkdir(parents=True, exist_ok=True)
        TOPUP.write_text(json.dumps({"after": newest["databaseId"], "at": time.time()}))
        usual = usual_minutes()
        say("cloud: the night's postings are being fetched on GitHub"
            + (f" (usually about {usual} min)" if usual else "") + "; they are added when it finishes")
    except (CloudError, OSError, subprocess.SubprocessError) as exc:
        say(f"cloud: could not ask GitHub for the night's postings ({str(exc)[:160]}); "
            "the list has last evening's")


def bring_in(say=print, wait_s: int = WAIT_S, poll_s: int = POLL_S, sleep=time.sleep,
             clock=time.time, trigger: bool | None = None) -> dict | None:
    """The GitHub fetch's postings, waiting for a run under way. None: fetch on this Mac instead.
    The wait is on the wall clock: a Mac asleep with its lid shut comes back to a deadline that
    has passed, and a run that finished meanwhile is still taken before giving up."""
    trigger = trigger_on() if trigger is None else trigger
    try:
        deadline, said_wait, asked_at = clock() + wait_s, False, None
        while True:
            runs = recent_runs()
            ready, going = pending(runs, today_only=not trigger)
            if ready:
                got = _take(ready, say)
                if got is None:
                    say("cloud: no GitHub fetch could be brought in — fetching on this Mac")
                    return None
                if trigger:
                    _ask_top_up(ready[-1], going, say)
                return got
            if not going:
                taken = _last_taken()
                if asked_at is None and any(r["databaseId"] == taken for r in runs if _counts(r, not trigger)):
                    say(f"cloud: today's GitHub fetch (run {taken}) is in already")
                    return _collected()
                if trigger and asked_at is None:
                    dispatch()
                    asked_at = clock()
                    say("cloud: no GitHub fetch to take — asked GitHub for one")
                elif asked_at is None or clock() - asked_at >= APPEAR_S or clock() >= deadline:
                    say("cloud: no GitHub fetch today — fetching on this Mac")
                    return None
                sleep(poll_s)
                continue
            if clock() >= deadline:
                say("cloud: the GitHub fetch is taking too long — fetching on this Mac")
                return None
            if not said_wait:
                started = _when(going[0]["createdAt"]).astimezone()
                usual = usual_minutes()
                say(f"cloud: waiting for today's GitHub fetch (started {started:%H:%M}"
                    + (f", usually about {usual} min" if usual else "") + ") instead of fetching again")
                said_wait = True
            sleep(poll_s)
    except (CloudError, OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"cloud: could not reach the GitHub fetch ({str(exc)[:160]}) — fetching on this Mac")
        return None


def _night(ask: dict) -> tuple[list[dict], list[dict]]:
    """(the night's fetch, finished and not yet taken; fetches under way). A run a morning run took
    meanwhile is not in the first: `pending` leaves out every run already brought in."""
    ready, going = pending(recent_runs(), today_only=False)
    return [r for r in ready if r["databaseId"] > int(ask.get("after") or 0)], going


def wait_top_up(say=print, wait_s: int = TOPUP_WAIT_S, poll_s: int = POLL_S, sleep=time.sleep,
                clock=time.time) -> bool:
    """Wait for the night's fetch, asked for by the morning run, holding no lock and opening no
    database: Start, a fetch or scoring from the web app, and the board search all go on meanwhile
    (devops/daily_pipeline.sh topup). True: it finished, and the ask is kept for `top_up`, which
    brings it in under the morning lock. False: nothing to add, and the ask is dropped -- it is
    stale, the fetch failed or never came, took too long, or GitHub is out of reach."""
    if not TOPUP.exists():
        return False
    keep = False
    try:
        ask = json.loads(TOPUP.read_text())
        if clock() - float(ask.get("at") or 0) > TOPUP_STALE_H * 3600:
            say("cloud: the night's fetch was asked for too long ago — not waiting for it")
            return False
        start = clock()
        deadline = start + wait_s
        said = False
        while True:
            ready, going = _night(ask)
            if ready:
                keep = True
                say("cloud: the night's GitHub fetch has finished — adding it")
                return True
            if not going and clock() - start >= APPEAR_S:
                say("cloud: the night's GitHub fetch did not run or did not finish — the list stays as it is")
                return False
            if clock() >= deadline:
                say("cloud: the night's GitHub fetch is taking too long — the list stays as it is")
                return False
            if not said:
                say("cloud: waiting for the night's GitHub fetch")
                said = True
            sleep(poll_s)
    except (CloudError, OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        say(f"cloud: could not reach the night's GitHub fetch ({str(exc)[:160]}) — the list stays as it is")
        return False
    finally:
        if not keep:
            TOPUP.unlink(missing_ok=True)


def top_up(say=print) -> dict | None:
    """The night's fetch brought in, once `wait_top_up` found it finished. Called under the
    morning lock (main.py daily --top-up, run by devops/daily_pipeline.sh as a morning run of its
    own), so it never merges beside a morning run or another top-up, and it waits for nothing. A
    run taken meanwhile is not taken twice. The ask is dropped either way: a run that could not be
    brought in is left for the next morning run, as any other. None: nothing was added."""
    if not TOPUP.exists():
        return None
    try:
        ready, _ = _night(json.loads(TOPUP.read_text()))
        if not ready:
            say("cloud: the night's GitHub fetch is in already — nothing to add")
            return None
        return _take(ready, say)
    except (CloudError, OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        say(f"cloud: could not reach the night's GitHub fetch ({str(exc)[:160]}) — the list stays as it is")
        return None
    finally:
        TOPUP.unlink(missing_ok=True)


def evening(say=print, now: datetime | None = None) -> str:
    """The evening fetch, started from this Mac at 8 pm (a launchd job, run on waking when the
    Mac slept through 8). Nothing after midnight: the morning run covers it. Nothing while a run
    is under way, or when one finished in the last four hours. Returns what it did."""
    if not set_up():
        return "not set up"
    if not trigger_on():
        say("cloud: the evening GitHub fetch is switched off (cloud.trigger_runs in config.yaml)")
        return "off"
    now = now or datetime.now().astimezone()
    if not EVENING[0] <= now.hour < EVENING[1]:
        say(f"cloud: {now:%H:%M} is outside 8 pm to midnight — the morning run fetches instead")
        return "outside"
    runs = recent_runs()
    if any(r["status"] != "completed" for r in runs if _hours_ago(r["createdAt"], now) <= FRESH_H):
        say("cloud: a GitHub fetch is under way already — not starting another")
        return "running"
    if any(r["status"] == "completed" and r.get("conclusion") == "success"
           and _hours_ago(r.get("updatedAt") or r["createdAt"], now) < 4 for r in runs):
        say("cloud: a GitHub fetch finished this evening already — not starting another")
        return "done"
    dispatch()
    say("cloud: started the evening GitHub fetch")
    return "asked"


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def push_boards(say=print) -> bool:
    """Send the board list to the cloud, encrypted, when it changed. Returns whether it was sent."""
    if not BOARDS.exists():
        return False
    mark = _fingerprint(BOARDS)
    try:
        if BOARDS_SENT.read_text().strip() == mark:
            return False
    except OSError:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        locked = Path(tmp) / BOARDS_ASSET
        _openssl(["-salt", "-in", str(BOARDS), "-out", str(locked)], _passphrase())
        try:
            _gh("release", "view", RELEASE)
        except CloudError:
            _gh("release", "create", RELEASE, "--title", "Board list for the daily cloud fetch (encrypted)",
                "--notes", "Read by .github/workflows/daily-scrape.yml. Locked with the repository's "
                           "RESULTS_PASSPHRASE secret; it cannot be read without it.", "--latest=false")
        _gh("release", "upload", RELEASE, str(locked), "--clobber", timeout=600)
    BOARDS_SENT.parent.mkdir(parents=True, exist_ok=True)
    BOARDS_SENT.write_text(mark + "\n")
    rows = sum(1 for _ in BOARDS.open(encoding="utf-8", errors="replace")) - 1
    say(f"cloud: sent the board list ({rows:,} boards, encrypted) for the next GitHub fetch")
    return True


def set_up() -> bool:
    """Whether this copy has a GitHub fetch at all: a passphrase in .env, gh, and a git checkout
    for gh to ask about. A copy installed from the Mac app has none of these, and its morning
    run reads the boards on the Mac without a word about GitHub."""
    try:
        _passphrase()
        _tool("gh")
    except CloudError:
        return False
    return (PROJECT_ROOT / ".git").exists()


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "state"
    try:
        if command == "state":
            print(state())
        elif command == "collect":
            runs = recent_runs()
            ready, _ = pending(runs, today_only=not trigger_on())
            if not ready:
                print(f"nothing to bring in: {state(runs)}")
                return 1
            print(json.dumps(_take(ready, print)))
        elif command == "wait-top-up":
            # Exit 0: the night's fetch is ready to bring in. 1: nothing to add.
            if not set_up():
                return 1
            return 0 if wait_top_up(say=lambda line: print(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}", flush=True)) else 1
        elif command == "evening":
            evening(say=lambda line: print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}", flush=True))
        elif command == "push-boards":
            if not set_up():
                print("no GitHub fetch is set up here; the board list stays on this Mac")
            elif not push_boards():
                print("the board list has not changed since it was last sent")
        else:
            print(__doc__)
            return 2
    except CloudError as exc:
        print(f"cloud: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
