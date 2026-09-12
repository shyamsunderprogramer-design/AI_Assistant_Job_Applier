"""Serve the job console: write the status file the page polls.

The numbers come from status.collect(), the same source `main.py status` uses,
so the terminal view and the browser page can never disagree. This module only
turns those tasks into JSON and keeps the served directory stocked.

Long-running: it holds status.py in memory, so restart it after changing how a
task is measured or the page will keep serving the old reading.
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, "data", "console")
OUT = os.path.join(SERVE, "status.json")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")

sys.path.insert(0, ROOT)

LOGS = {
    "Company discovery": "data/discovery_run.log",
    "Mailbox scan": "data/mailscan.log",
    "Website enrichment": "data/companies_build/enrich_loop.log",
}


def snapshot(cfg):
    import status

    jobs = []
    for task in status.collect(cfg):
        jobs.append({
            "name": task.name,
            "detail": task.detail,
            "done": task.done,
            "total": task.total,
            "pct": round(task.pct, 1),
            "alive": task.running,
            "log": LOGS.get(task.name, ""),
        })
    return {"updated": time.time(), "jobs": jobs}


def main():
    from config.loader import load_config
    from db.session import init_engine

    cfg = load_config()
    init_engine(cfg.database_url)

    os.makedirs(SERVE, exist_ok=True)
    # The page is tracked in tools/; data/ is gitignored and holds only the
    # served copy, so the server never has the project root in reach.
    shutil.copyfile(PAGE, os.path.join(SERVE, "index.html"))

    while True:
        try:
            data = snapshot(cfg)
        except Exception as exc:            # a transient DB lock must not kill the writer
            data = {"updated": time.time(), "jobs": [], "error": str(exc)}
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, OUT)                # readers never see a half-written file
        time.sleep(5)


if __name__ == "__main__":
    main()
