"""Nightly backup and weekly health report (devops/)."""

import gzip
import os
import sqlite3
import time
from datetime import date, timedelta

from devops import backup_db, health_report


def make_db(path, rows=3):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (x)")
        db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])


def project(tmp_path):
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "backend" / "config").mkdir(parents=True)
    make_db(root / "data" / "jobs.db")
    make_db(root / "data" / "companies.db")
    (root / "backend" / "config" / "applicant.yaml").write_text("name: x\n")
    return root


def test_backup_is_a_readable_copy_with_the_personal_files(tmp_path):
    root, dest = project(tmp_path), tmp_path / "backups"
    out = backup_db.run(dest, keep=14, root=root)
    assert out["ok"] and {"jobs.db.gz", "companies.db.gz", "applicant.yaml"} <= set(out["saved"])
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress((dest / date.today().isoformat() / "jobs.db.gz").read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT count(*) FROM t").fetchone()[0] == 3


def test_unchanged_companies_db_is_not_copied_again(tmp_path):
    root, dest = project(tmp_path), tmp_path / "backups"
    backup_db.run(dest, keep=14, root=root)
    os.utime(root / "data" / "companies.db", (time.time() - 3600,) * 2)
    for f in dest.glob("*/companies.db.gz"):
        os.utime(f, None)
    assert "companies.db.gz" not in backup_db.run(dest, keep=14, root=root)["saved"]


def test_old_nights_are_pruned_but_the_last_companies_copy_is_kept(tmp_path):
    dest = tmp_path / "backups"
    for d in range(5):
        night = dest / f"2026-09-0{d + 1}"
        night.mkdir(parents=True)
        (night / "jobs.db.gz").write_bytes(b"x")
    (dest / "2026-09-01" / "companies.db.gz").write_bytes(b"x")
    gone = backup_db.prune(dest, keep=2)
    assert gone == ["2026-09-02", "2026-09-03"]          # 09-01 holds the only companies copy
    assert sorted(p.name for p in dest.iterdir()) == ["2026-09-01", "2026-09-04", "2026-09-05"]


def write_log(root, lines):
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "daily_run.log").write_text("\n".join(lines) + "\n")


def test_runs_are_counted_on_the_day_they_started_and_name_the_failed_stage(tmp_path):
    d0 = date.today() - timedelta(days=3)
    d1, d2 = d0 + timedelta(days=1), d0 + timedelta(days=2)
    write_log(tmp_path, [
        f"{d0} 06:00:00  === daily starting ===",
        f"{d1} 18:00:00  === daily finished, exit 0 ===",          # 36 hours: belongs to d0
        f"{d2} 06:00:00  === daily starting ===",
        "  adzuna: ReadTimeout: HTTPSConnectionPool(host='api.adzuna.com')",
        f"{d2} 09:00:00  === daily finished, exit 1 ===",
    ])
    runs = health_report.morning_runs(tmp_path, d0)
    assert runs[str(d0)] == {"exit": 0, "hours": 36.0, "failed": []}
    assert runs[str(d1)] is None
    assert runs[str(d2)]["failed"] == ["adzuna (ReadTimeout)"]
    found = health_report.problems({"runs": runs, "scrapes": [], "failing": [],
                                    "backup": {"hours": 2}, "free_gb": 100})
    assert any("took 36 hours" in p for p in found)
    assert any(f"No morning run started on {d1}" in p for p in found)
    assert any("adzuna (ReadTimeout); the rest ran" in p for p in found)


def test_a_quiet_week_has_no_problems_and_a_stale_backup_is_one():
    today = str(date.today())
    base = {"runs": {today: None}, "scrapes": [{"day": today, "boards": 100, "failed": 1, "new": 3}],
            "failing": [], "backup": {"hours": 5}, "free_gb": 100}
    assert health_report.problems(base) == []
    assert health_report.problems({**base, "backup": {"hours": 50}}) == ["The last backup is 50 hours old."]
    assert health_report.problems({**base, "backup": None}) == ["No database backup found."]
