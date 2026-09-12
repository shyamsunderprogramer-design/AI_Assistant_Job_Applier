"""Write data/console/status.json every few seconds so the console page can poll it.

Counters restart at zero when a job is relaunched, and the old run's lines stay
in the log. So we never just tail: we take the last value *after the last
decrease*, which is where the current run began.
"""
import json
import os
import re
import shutil
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, "data", "console")
OUT = os.path.join(SERVE, "status.json")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")


def current_run_values(path, pattern):
    """Every (done, total) in the log, keeping only the current run's tail."""
    try:
        with open(path, errors="replace") as fh:
            values = [(int(a), int(b)) for a, b in re.findall(pattern, fh.read())]
    except FileNotFoundError:
        return None
    if not values:
        return None
    start = 0
    for i in range(1, len(values)):
        if values[i][0] < values[i - 1][0]:   # counter went backwards: new run
            start = i
    return values[-1] if start <= len(values) - 1 else None


# A shell running our own tooling mentions these job names on its command line,
# so match real command lines ourselves instead of trusting `pgrep -f`.
WRAPPERS = ("shell-snapshots", "/bin/zsh -c", "/bin/bash -c", "pgrep", "jobstatus")


def running(pattern):
    out = subprocess.run(["ps", "-eo", "pid=,command="],
                         capture_output=True, text=True).stdout
    pids = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        pid, _, command = line.partition(" ")
        if any(w in command for w in WRAPPERS):
            continue
        if re.search(pattern, command):
            pids.append(int(pid))
    return pids


def started_at(pids):
    """Wall-clock start of the oldest matching process, or None."""
    if not pids:
        return None
    args = ["ps", "-o", "etime=", "-p", ",".join(str(p) for p in pids)]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    oldest = None
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        days, _, rest = line.partition("-")
        if not rest:
            days, rest = "0", days
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        secs = int(days) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
        oldest = secs if oldest is None else max(oldest, secs)
    return None if oldest is None else time.time() - oldest


def is_stale(log, pids):
    """True when the log has not been written since the process started."""
    start = started_at(pids)
    if start is None:
        return False
    try:
        # Grace: a job that writes a banner as it starts is not stale.
        return os.path.getmtime(log) < start - 3
    except FileNotFoundError:
        return True


def count(path, needle):
    try:
        with open(path, errors="replace") as fh:
            return sum(1 for line in fh if needle in line)
    except FileNotFoundError:
        return 0


def snapshot():
    data = os.path.join(ROOT, "data")
    mail_log = os.path.join(data, "mailscan.log")
    disc_log = os.path.join(data, "discovery_run.log")
    enr_log = os.path.join(data, "companies_build", "enrich_loop.log")

    mail = current_run_values(mail_log, r"(\d+)/(\d+) messages")
    disc = current_run_values(disc_log, r"(\d+)/(\d+) names")

    try:
        with open(enr_log, errors="replace") as fh:
            rounds = re.findall(r"round (\d+)", fh.read())
        enr_round = int(rounds[-1]) if rounds else 0
    except FileNotFoundError:
        enr_round = 0

    jobs = [
        {
            "name": "scan-mail",
            "detail": "harvesting company names from the mailbox",
            "pids": running(r"main\.py scan-mail"),
            "done": mail[0] if mail else 0,
            "total": mail[1] if mail else 0,
            "extra": f"{count(mail_log, 'names,')} progress lines",
            "log": "data/mailscan.log",
        },
        {
            "name": "discover",
            "detail": "probing Greenhouse / Lever / Ashby for job boards",
            "pids": running(r"main\.py discover"),
            "done": disc[0] if disc else 0,
            "total": disc[1] if disc else 0,
            "extra": f"{count(disc_log, 'Found ')} boards found (all runs)",
            "log": "data/discovery_run.log",
        },
        {
            "name": "enrich",
            "detail": "guessing websites + careers URLs",
            "pids": running(r"enrich_loop\.sh"),
            "done": enr_round,
            "total": 60,
            "extra": f"round {enr_round} of 60",
            "log": "data/companies_build/enrich_loop.log",
        },
    ]
    for job, log in zip(jobs, (mail_log, disc_log, enr_log)):
        job["alive"] = bool(job["pids"])
        # A relaunched job has not written yet; the tail still holds the dead
        # run's numbers, so say "starting" rather than report them as current.
        job["stale"] = job["alive"] and is_stale(log, job["pids"])
        if job["stale"]:
            job["done"] = 0
    return {"updated": time.time(), "jobs": jobs}


def main():
    os.makedirs(SERVE, exist_ok=True)
    # The page is tracked in tools/; data/ is gitignored and holds only the
    # served copy, so the server never has the project root in reach.
    shutil.copyfile(PAGE, os.path.join(SERVE, "index.html"))
    while True:
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot(), fh)
        os.replace(tmp, OUT)       # readers never see a half-written file
        time.sleep(5)


if __name__ == "__main__":
    main()
