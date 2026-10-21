"""Today's cloud fetch, brought into this Mac's database -- or a fetch here when there is none.

GitHub Actions fetches every board early each morning, whether the laptop is open, shut or off
(.github/workflows/daily-scrape.yml), and leaves the postings on the run, encrypted. The morning
run here (devops/daily_pipeline.sh -> main.py daily --cloud) then:

  * takes that run's postings when it has finished -- minutes, where a fetch here takes hours;
  * waits for it when it is under way, rather than fetching the same boards twice;
  * fetches on this Mac when GitHub has no run today (it skips scheduled runs when it is busy).

The cloud reads its boards from a list kept out of the public repository: this Mac sends
backend/config/boards.csv as an encrypted release asset whenever the list changes, and the run
unlocks it with the same passphrase. Both directions use RESULTS_PASSPHRASE from .env, which the
GitHub secret of the same name holds too.

    python -m devops.cloud_results state         # ready | running | collected | none
    python -m devops.cloud_results collect       # bring in today's run, if it finished
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
WAIT_S = 3 * 3600                              # longest wait for a run under way, before fetching here
POLL_S = 120
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


def today_run() -> dict | None:
    """Today's run of the cloud fetch (today where you are), the latest if several."""
    runs = json.loads(_gh("run", "list", "--workflow", WORKFLOW, "--limit", "8", "--json",
                          "databaseId,status,conclusion,createdAt,updatedAt") or "[]")
    today = datetime.now().date()
    mine = [r for r in runs if _local_day(r["createdAt"]) == today]
    return max(mine, key=lambda r: r["createdAt"]) if mine else None


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


def state(run: dict | None = None) -> str:
    """ready (finished, not yet here), collected (already here), running, or none (no run today,
    or one that failed)."""
    run = run if run is not None else today_run()
    if not run:
        return "none"
    if run["status"] != "completed":
        return "running"
    if run.get("conclusion") != "success":
        return "none"
    return "collected" if _collected().get("run") == run["databaseId"] else "ready"


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
        # Into the live file, in one transaction: a failure leaves nothing half-merged.
        result = merge(db, PROJECT_ROOT / "data" / "jobs.db", search=search)
    result.update(run=run_id, boards=boards, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    COLLECTED.parent.mkdir(parents=True, exist_ok=True)
    COLLECTED.write_text(json.dumps(result))
    return result


def bring_in(say=print, wait_s: int = WAIT_S, poll_s: int = POLL_S, sleep=time.sleep) -> dict | None:
    """Today's cloud postings, waiting for a run under way. None: fetch on this Mac instead."""
    try:
        deadline, said_wait = time.monotonic() + wait_s, False
        while True:
            run = today_run()
            now = state(run)
            if now == "collected":
                say(f"cloud: today's GitHub fetch (run {run['databaseId']}) is in already")
                return _collected()
            if now == "ready":
                say(f"cloud: bringing in today's GitHub fetch (run {run['databaseId']})…")
                got = collect(run["databaseId"], run["createdAt"])
                news = "".join(f", {got[k]:,} {what}" for k, what in (("edited", "edited by the employer"),
                                                                     ("typed", "given a job type by their board"))
                               if got.get(k))
                left = f" ({got['skipped']:,} more your search leaves out)" if got.get("skipped") else ""
                say(f"cloud: {got['added']:,} new postings{left}, {got['refreshed']:,} seen again{news}, "
                    f"{got.get('closed', 0):,} taken down, from {got['boards']:,} boards")
                return got
            if now == "none":
                say("cloud: no GitHub fetch today — fetching on this Mac")
                return None
            if time.monotonic() >= deadline:
                say("cloud: the GitHub fetch is taking too long — fetching on this Mac")
                return None
            if not said_wait:
                started = datetime.fromisoformat(run["createdAt"].replace("Z", "+00:00")).astimezone()
                usual = usual_minutes()
                say(f"cloud: waiting for today's GitHub fetch (started {started:%H:%M}"
                    + (f", usually about {usual} min" if usual else "") + ") instead of fetching again")
                said_wait = True
            sleep(poll_s)
    except (CloudError, OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"cloud: could not reach the GitHub fetch ({str(exc)[:160]}) — fetching on this Mac")
        return None


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
            run = today_run()
            if state(run) != "ready":
                print(f"nothing to bring in: {state(run)}")
                return 1
            print(json.dumps(collect(run["databaseId"], run["createdAt"])))
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
