"""Build the status document the published console reads.

Prints one JSON object on stdout. It is pushed to the artifact database from
the Claude session, because a page on claude.ai cannot reach this machine.
"""
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    from config.loader import load_config
    from db.models import Company, Job
    from db.session import get_session, init_engine
    import status

    cfg = load_config()
    init_engine(cfg.database_url)

    jobs = [
        {"name": t.name, "done": t.done, "total": t.total,
         "alive": t.running, "detail": t.detail}
        for t in status.collect(cfg)
    ]

    with get_session() as session:
        boards = session.query(Company).count()
        open_roles = session.query(Job).filter(Job.is_open.is_(True)).count()
        strong = session.query(Job).filter(
            Job.is_open.is_(True), Job.ats_match_score >= 0.70
        ).count()

    counters = {"boards": boards, "open": open_roles, "strong": strong}
    try:
        from companies.db import DB_PATH
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        counters["sites"] = c.execute(
            "SELECT COUNT(*) FROM companies WHERE website IS NOT NULL").fetchone()[0]
        counters["careers"] = c.execute(
            "SELECT COUNT(*) FROM companies WHERE careers_url IS NOT NULL").fetchone()[0]
        c.close()
    except sqlite3.Error:
        pass        # company DB busy: publish the rest rather than nothing

    print(json.dumps({"updated": int(time.time()), "jobs": jobs, "counters": counters}))


if __name__ == "__main__":
    main()
