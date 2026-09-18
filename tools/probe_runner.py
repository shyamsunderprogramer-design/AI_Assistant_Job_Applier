"""Walk the whole refined company list, for as many days as it takes.

1.75 million names is not a job that finishes in one sitting. It has to survive
a closed lid, a dropped wifi connection, a reboot and being stopped halfway,
and pick up where it left off every time. That is what this is for.

**Resuming is free, and that is the important design fact.** Every probe ever
sent is recorded in `probe_log`, and the discovery code checks that table
before asking anything. So a restart does not need a bookmark, a checkpoint
file, or a byte offset: it re-reads the list from the top and the first fifty
thousand names cost nothing because they are answered from cache. Measured on
a 55-name sample -- first run 132 probes sent, second run 0 sent, 165 from
cache. Nothing here can lose work, because the work is saved as it happens.

What this adds on top is the ability to leave it alone:

  * it holds the Mac awake while it runs, and lets it sleep the moment it stops
  * it waits out a lost internet connection instead of dying on it
  * it writes what it is doing to a state file, so a page can show progress
  * it stops cleanly when asked, mid-batch, without losing the batch

    python -m tools.probe_runner                # start, resume, keep going
    python -m tools.probe_runner --status       # what is it doing
    python -m tools.probe_runner --stop         # ask it to stop
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NAMES = Path("data/refined_names_all.txt")
STATE = Path("data/probe_state.json")
PIDFILE = Path("data/probe_runner.pid")
LOG = Path("data/probe_runner.log")
STOPFILE = Path("data/probe_runner.stop")

# Names handed to the discovery pass at a time. Small enough that the progress
# file moves often: at 200 the bar sat on one number for seven minutes and read
# as frozen, which is how a working run gets reported as broken -- twice.
BATCH = 40

# When the network is gone there is nothing to do but wait. Back off rather
# than hammer, and never give up -- a laptop reconnects eventually.
NET_WAIT_START_S = 30
NET_WAIT_MAX_S = 900


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def online(host: str = "boards.greenhouse.io") -> bool:
    """Is there internet? Cheaper and more honest than catching it per request."""
    try:
        socket.create_connection((host, 443), timeout=8).close()
        return True
    except OSError:
        return False


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(**fields) -> None:
    state = read_state()
    state.update(fields, updated_at=now())
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE)          # atomic, so a reader never sees half a file


def running_pid() -> int | None:
    """The pid of a live runner, or None. A zombie counts as not running."""
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        if state.startswith("Z"):
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    return pid


def load_names(path: Path) -> list[str]:
    from scraper.discovery import load_names_from_file

    return load_names_from_file(path)


def hold_awake() -> subprocess.Popen | None:
    """Keep the Mac from sleeping for as long as this process lives.

    Without it the run stops sixty seconds after the last keypress, which on
    this machine is what `sleep 1` means. The assertion dies with the process,
    so a crash does not leave the Mac awake forever.
    """
    try:
        return subprocess.Popen(
            ["caffeinate", "-imsw", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        return None


def run(names_path: Path = NAMES, *, batch: int = BATCH,
        max_slugs: int = 1, limit: int | None = None) -> int:
    from config.loader import load_config
    from db.session import init_engine

    if running_pid():
        log(f"Already running as pid {running_pid()} — nothing to do.")
        return 0

    STOPFILE.unlink(missing_ok=True)
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(f"{os.getpid()}\n")
    awake = hold_awake()

    cfg = load_config()
    init_engine(cfg.database_url)

    names = load_names(names_path)
    if limit:
        names = names[:limit]
    total = len(names)
    log(f"=== probe runner starting: {total:,} names, batches of {batch} ===")
    if awake is None:
        log("WARNING: caffeinate unavailable — the Mac may sleep and pause this.")

    started = time.time()
    found_total = int(read_state().get("found_total") or 0)
    done = 0
    stopping = False

    def ask_to_stop(signum, frame):
        nonlocal stopping
        stopping = True
        log("Stop requested — finishing the current batch first.")

    signal.signal(signal.SIGTERM, ask_to_stop)
    signal.signal(signal.SIGINT, ask_to_stop)

    write_state(status="running", pid=os.getpid(), total=total, done=0,
                found_total=found_total, started_at=now(), names_file=str(names_path))

    from scraper.discovery import CompanyDiscoverer
    from scraper.http_client import HttpSettings, PoliteClient
    from scraper.runner import build_scrapers

    sources = cfg.get("discovery.probe_sources", [])
    # One client and one discoverer for the whole run, not one per batch: the
    # discoverer holds an in-memory cache of (source, slug) pairs it has
    # already decided about, and rebuilding it every two hundred names would
    # throw that away.
    client = PoliteClient(HttpSettings.from_config(cfg))
    discoverer = CompanyDiscoverer(
        scrapers=build_scrapers(cfg, client),
        strip_suffixes=cfg.get("discovery.strip_suffixes", []),
    )

    for start in range(0, total, batch):
        if stopping or STOPFILE.exists():
            log("Stopping as asked.")
            break

        chunk = names[start:start + batch]

        # Wait out a dead connection rather than burning through the list
        # recording thousands of false misses.
        wait = NET_WAIT_START_S
        while not online():
            write_state(status="offline", done=done)
            log(f"No internet — waiting {wait}s.")
            time.sleep(wait)
            wait = min(wait * 2, NET_WAIT_MAX_S)
            if stopping or STOPFILE.exists():
                break
        if stopping or STOPFILE.exists():
            break

        try:
            before = discoverer.progress.found
            discoverer.discover_from_names(chunk, sources, max_slugs=max_slugs)
            found = discoverer.progress.found - before
        except KeyboardInterrupt:
            log("Interrupted.")
            break
        except Exception as exc:
            # One bad batch must not end a five-day run. The names in it stay
            # unprobed and will be retried on the next pass.
            log(f"Batch at {start} failed ({type(exc).__name__}: {exc}) — continuing.")
            found = 0

        done = min(start + len(chunk), total)
        found_total += found

        elapsed = time.time() - started
        rate = done / elapsed if elapsed > 0 else 0
        eta_h = (total - done) / rate / 3600 if rate > 0 else None
        write_state(status="running", done=done, total=total,
                    found_total=found_total,
                    names_per_hour=round(rate * 3600),
                    eta_hours=round(eta_h, 1) if eta_h else None)

        if start % (batch * 25) == 0 or found:
            eta = f", ~{eta_h:,.0f}h left" if eta_h else ""
            log(f"{done:,}/{total:,} names · {found_total:,} boards found · "
                f"{rate*3600:,.0f}/hr{eta}")

    write_state(status="stopped" if (stopping or STOPFILE.exists()) else "finished",
                done=done, found_total=found_total)
    log(f"=== stopped: {done:,}/{total:,} names, {found_total:,} boards found ===")
    log("Resuming later costs nothing — probes already sent are answered from cache.")

    PIDFILE.unlink(missing_ok=True)
    STOPFILE.unlink(missing_ok=True)
    if awake is not None:
        awake.terminate()
    return 0


def status() -> int:
    state = read_state()
    pid = running_pid()
    if not state:
        print("  never run")
        return 0
    done, total = state.get("done", 0), state.get("total", 0)
    pct = f"{done / total * 100:.1f}%" if total else "—"
    print(f"  status      : {'RUNNING (pid %d)' % pid if pid else state.get('status', '?')}")
    print(f"  progress    : {done:,} of {total:,}  ({pct})")
    print(f"  boards found: {state.get('found_total', 0):,}")
    if state.get("names_per_hour"):
        print(f"  rate        : {state['names_per_hour']:,} names/hour")
    if state.get("eta_hours"):
        print(f"  remaining   : ~{state['eta_hours']:,} hours")
    print(f"  updated     : {state.get('updated_at', '?')}")
    return 0


def stop() -> int:
    pid = running_pid()
    if not pid:
        print("  not running")
        return 0
    # A file as well as a signal: the signal handler finishes the current batch,
    # and the file survives if the signal is missed.
    STOPFILE.write_text(now())
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    print(f"  asked pid {pid} to stop after its current batch")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--names", type=Path, default=NAMES)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--max-slugs", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args(argv[1:])

    if args.status:
        return status()
    if args.stop:
        return stop()
    return run(args.names, batch=args.batch, max_slugs=args.max_slugs, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
