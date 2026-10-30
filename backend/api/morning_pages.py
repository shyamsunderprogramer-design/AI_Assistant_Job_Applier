"""The morning run, by hand: start it, stop it, and switch its 6 AM start on or off.

The morning run is devops/daily_pipeline.sh: new postings from every board, scored
against the resume, then skills and applications prepared for the best of them.
launchd starts it at 6 AM. These are the person's own controls over it, on the
Jobs page:

  * Start runs it now, whatever the time and whether or not the 6 AM start is on;
  * Stop ends it -- or a fetch or re-score started from the page -- keeping what it
    has already fetched;
  * the switch turns the 6 AM start off (the scheduled tick then does nothing) or
    back on. Off, the run happens only when Start is pressed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime

from flask import jsonify, request

from backend.api import app as webapp
from backend.api.app import DAILY_LOG, PROJECT_ROOT, app, task_pid

SCRIPT = PROJECT_ROOT / "devops" / "daily_pipeline.sh"
LOCK = PROJECT_ROOT / "data" / "daily.lock"
PIDFILE = PROJECT_ROOT / "data" / "daily.pid"              # the script's own pid, beside its lock
AUTO_OFF = PROJECT_ROOT / "data" / "daily_auto_off"      # read by daily_pipeline.sh
OUT = PROJECT_ROOT / "data" / "launchd_daily.log"           # where launchd sends the script's own output
PAGE_TASKS = ("scrape", "score")                            # page-started work Stop also ends


def pipeline_pids() -> list[int]:
    """The morning run's own process: the script, however it was started."""
    return webapp.pipeline_pids("daily") or []


def _say(line: str) -> None:
    """A line in the run's own log, where the page reads its state from."""
    try:
        with DAILY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


def last_run() -> dict | None:
    """When the latest morning run started and how it ended, from the log."""
    try:
        size = DAILY_LOG.stat().st_size
        with DAILY_LOG.open("rb") as handle:
            handle.seek(max(0, size - 6_000_000))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    start = text.rfind("=== daily starting ===")
    if start == -1:
        return None
    stamp = lambda at: text[text.rfind("\n", 0, at) + 1:at].strip()[:19]  # noqa: E731
    run = {"started": stamp(start), "ended": None, "how": None}
    for marker, how in (("=== daily finished, exit 0 ===", "finished"), ("=== daily finished, exit", "failed"),
                        ("=== daily stopped", "stopped")):
        at = text.find(marker, start)
        if at != -1:
            run.update(ended=stamp(at), how=how)
            break
    return run


def not_ready() -> str | None:
    """Why a morning run should wait for the person: no resume yet, or no search made from it
    (backend/core/setup_state.py). A new install's first morning is the person's own first step."""
    from backend.core.setup_state import why_not_ready
    return why_not_ready(webapp.cfg())


def missed_morning(now: datetime | None = None) -> bool:
    """Today's 6 AM start never happened. launchd runs a start missed while the Mac slept as soon
    as it wakes, but not one missed while it was off: then nothing runs until this notices."""
    now = now or datetime.now()
    if (now.hour, now.minute) < (6, 5) or AUTO_OFF.exists() or LOCK.is_dir() or pipeline_pids():
        return False
    if not_ready():
        return False
    last = last_run()
    return not (last and str(last.get("started") or "").startswith(now.strftime("%Y-%m-%d")))


def catch_up() -> bool:
    """Start the morning run the Mac missed, as the schedule would have. Returns whether it did."""
    if not missed_morning():
        return False
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as out:
        subprocess.Popen(["/bin/bash", str(SCRIPT), "daily"], cwd=PROJECT_ROOT, stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "CATCH_UP": "1"})
    return True


def watch_mornings(every_s: int = 900) -> None:
    """Look for a missed morning when the app starts (a Mac switched on after 6 AM) and every
    quarter of an hour after. Started by backend/api/__main__.py, never on import."""
    import logging
    import threading

    from backend.core.startup import watcher_startup_held
    if watcher_startup_held("morning-catch-up"):
        return

    def loop():
        time.sleep(60)                     # the app first; a morning run is not in a hurry
        while True:
            try:
                if catch_up():
                    logging.getLogger(__name__).info("Started the morning run the Mac missed")
            except Exception as exc:       # one bad look never stops the next
                logging.getLogger(__name__).warning("Morning catch-up: %s", exc)
            time.sleep(every_s)

    threading.Thread(target=loop, name="morning-catch-up", daemon=True).start()


@app.get("/run/morning")
def morning_state():
    running = bool(pipeline_pids())
    page_task = next((t for t in PAGE_TASKS if task_pid(t)), None)
    return jsonify(ok=True, running=running, page_task=page_task, auto=not AUTO_OFF.exists(),
                   last=last_run())


@app.post("/run/morning/start")
def morning_start():
    if pipeline_pids():
        return jsonify(ok=True, started=False, note="The morning run is already going.")
    busy = next((t for t in PAGE_TASKS if task_pid(t)), None)
    if busy:
        return jsonify(ok=False, error=f"A {'fetch' if busy == 'scrape' else 're-score'} you started from "
                                       "this page is running. Stop it, or let it finish, first."), 409
    waiting = not_ready()
    if waiting:
        return jsonify(ok=False, error=f"Not yet: {waiting}."), 409
    _clear_lock()                          # left by a run that was killed; nothing holds it
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as out:
        subprocess.Popen(["/bin/bash", str(SCRIPT), "daily"], cwd=PROJECT_ROOT, stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         # Its own session: restarting this app must not end the run.
                         start_new_session=True, env={**os.environ, "MANUAL": "1"})
    return jsonify(ok=True, started=True)


def _clear_lock() -> None:
    try:
        LOCK.rmdir()
    except OSError:
        pass
    PIDFILE.unlink(missing_ok=True)


def _alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except OSError:
        return False
    return True


@app.post("/run/morning/stop")
def morning_stop():
    """End the morning run (and a fetch or re-score started from the page): the script and
    everything it started, which share its process group. What it fetched so far is kept --
    every board is saved as it is read."""
    own = os.getpgid(0)
    morning = bool(pipeline_pids()) or LOCK.is_dir()
    groups: set[int] = set()
    for pid in pipeline_pids() + [p for p in (task_pid(t) for t in PAGE_TASKS) if p]:
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group != own:
            groups.add(group)
    if not groups and not LOCK.is_dir():
        return jsonify(ok=True, stopped=False, note="Nothing was running.")
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_alive(g) for g in groups):
        time.sleep(0.3)
    for group in groups:
        if _alive(group):
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
    # A script ended by a signal never runs its own clean-up, so the lock would stay and
    # hold the page at "running".
    _clear_lock()
    if morning:
        _say("=== daily stopped from the Jobs page ===")
    return jsonify(ok=True, stopped=True)


@app.post("/run/morning/auto")
def morning_auto():
    on = bool((request.get_json(silent=True) or {}).get("on"))
    if on:
        AUTO_OFF.unlink(missing_ok=True)
    else:
        AUTO_OFF.parent.mkdir(parents=True, exist_ok=True)
        AUTO_OFF.write_text(f"6 AM start switched off on the Jobs page, {datetime.now():%Y-%m-%d %H:%M}\n")
    return jsonify(ok=True, auto=on)
