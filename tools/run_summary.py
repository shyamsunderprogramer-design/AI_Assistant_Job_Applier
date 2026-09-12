"""One line of JSON describing the run that just finished.

A scheduler cannot read a digest. It needs to know, in a shape it can branch
on, whether the run worked and whether anything appeared that is worth
interrupting someone for.
"""

import json
import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(task: str, exit_code: int) -> int:
    from config.loader import load_config
    from db.models import Job, utcnow
    from db.session import get_session, init_engine
    from jobage import age_label
    from scraper.lifecycle import as_utc

    cfg = load_config()
    init_engine(cfg.database_url)
    threshold = float(cfg.get("resume.min_score", 0.45))
    cutoff = utcnow() - timedelta(hours=24)

    with get_session() as session:
        open_roles = session.query(Job).filter(Job.is_open.is_(True)).count()
        strong = (
            session.query(Job)
            .filter(Job.is_open.is_(True), Job.ats_match_score >= threshold)
            .count()
        )
        fresh = [
            j for j in session.query(Job)
            .filter(Job.is_open.is_(True), Job.ats_match_score >= threshold)
            .order_by(Job.ats_match_score.desc())
            .all()
            if as_utc(j.found_at) and as_utc(j.found_at) >= cutoff
        ]
        # Only the ones worth an interruption travel in the summary.
        highlights = [
            {
                "company": j.company,
                "title": j.title,
                "score": round((j.ats_match_score or 0) * 100),
                "age": age_label(j),
                "url": j.application_url,
            }
            for j in fresh[:10]
        ]

    print(json.dumps({
        "ok": exit_code == 0,
        "task": task,
        "exit": exit_code,
        "open": open_roles,
        "strong": strong,
        "new_strong": len(fresh),
        "notify": exit_code != 0 or bool(fresh),
        "highlights": highlights,
        "digest": os.path.join("data", "digest.txt"),
    }))
    return 0


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "daily"
    code = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    raise SystemExit(main(task, code))
