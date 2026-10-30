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


def test_runs_are_counted_on_the_day_they_started_and_name_the_failed_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(health_report, "sleep_log", lambda: "")      # not this Mac's own record
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
    assert {k: runs[str(d0)][k] for k in ("exit", "hours", "failed")} == {"exit": 0, "hours": 36.0, "failed": []}
    assert runs[str(d0)]["held"] == [str(d1)]                         # still going on d1's morning
    assert runs[str(d1)] == {"held_by": str(d0)}
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


def test_a_slow_run_says_the_mac_was_asleep(tmp_path, monkeypatch):
    d0 = date.today() - timedelta(days=2)
    write_log(tmp_path, [f"{d0} 06:00:00  === daily starting ===",
                         f"{d0} 18:00:00  === daily finished, exit 0 ==="])
    pmset = "\n".join([
        f"{d0} 06:05:00 -0500 Sleep               \tEntering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=active "
        f"Using Batt (Charge:40%) 18000 secs",
        f"{d0} 12:00:00 -0500 Sleep               \tEntering Sleep state due to 'Maintenance Sleep':TCPKeepAlive=active "
        f"Using Batt (Charge:9%) 18000 secs",
        f"{d0 - timedelta(days=1)} 07:00:00 -0500 Sleep  \tEntering Sleep state due to 'Idle Sleep' Using AC 99999 secs"])
    monkeypatch.setattr(health_report, "sleep_log", lambda: pmset)
    runs = health_report.morning_runs(tmp_path, d0)
    assert runs[str(d0)]["slept"] == {"hours": 10.0, "battery": True, "lid": True, "charge": 9, "charge_max": 40}
    found = health_report.problems({"runs": runs, "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100})
    assert any("asleep for 10 of them with the lid closed and on battery (down to 9%)" in p for p in found), found


def test_a_battery_that_never_fell_is_at_its_charge_not_down_to_it():
    # 15 Oct 2026: every sleep during the run was on battery at 100%; the report said "down to 100%".
    day = str(date.today() - timedelta(days=1))
    slept = {"hours": 9.5, "battery": True, "lid": True, "charge": 100, "charge_max": 100}
    found = health_report.problems({"runs": {day: {"exit": 0, "hours": 12.6, "failed": [], "slept": slept}},
                                    "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100})
    assert any("with the lid closed and on battery (at 100%)." in p for p in found), found
    assert not any("down to" in p or "hour and a half" in p for p in found), found


def test_a_run_that_died_holds_no_later_morning(tmp_path, monkeypatch):
    monkeypatch.setattr(health_report, "sleep_log", lambda: "")
    d0 = date.today() - timedelta(days=3)
    write_log(tmp_path, [f"{d0} 06:00:00  === daily starting ===",                 # never finished
                         f"{d0 + timedelta(days=2)} 06:00:00  === daily starting ===",
                         f"{d0 + timedelta(days=2)} 08:00:00  === daily finished, exit 0 ==="])
    runs = health_report.morning_runs(tmp_path, d0)
    assert runs[str(d0)]["held"] == []
    found = health_report.problems({"runs": runs, "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100})
    assert f"No morning run started on {d0 + timedelta(days=1)}." in found
    assert f"The morning run on {d0} stopped partway: the run log has no end for it." in found


def test_a_run_from_before_the_week_still_explains_the_mornings_it_held(tmp_path, monkeypatch):
    # The report of 18 Oct 2026 said only "No morning run started on 2026-10-12": the run
    # holding that morning had started on the 10th, before the report's seven days.
    monkeypatch.setattr(health_report, "sleep_log", lambda: "")
    since = date.today() - timedelta(days=4)
    write_log(tmp_path, [f"{since - timedelta(days=2)} 06:00:00  === daily starting ===",
                         f"{since} 09:00:00  === daily finished, exit 0 ===",
                         f"{since + timedelta(days=1)} 06:00:00  === daily starting ===",
                         f"{since + timedelta(days=1)} 08:00:00  === daily finished, exit 0 ==="])
    runs = health_report.morning_runs(tmp_path, since)
    assert runs[str(since)] == {"held_by": str(since - timedelta(days=2))}
    found = health_report.problems({"runs": runs, "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100})
    assert (f"No morning run started on {since}: the run from {since - timedelta(days=2)} was still going "
            f"(a new one waits for it).") in found


def test_a_run_stopped_from_the_jobs_page_is_stopped_not_still_running(tmp_path, monkeypatch):
    # 19 Oct 2026: the table said "Still running" for a run stopped at 09:31.
    monkeypatch.setattr(health_report, "sleep_log", lambda: "")
    d0 = date.today() - timedelta(days=2)
    write_log(tmp_path, [f"{d0} 06:00:00  === daily starting ===",
                         f"{d0} 09:30:00  === daily stopped from the Jobs page ===",
                         f"{d0 + timedelta(days=1)} 06:00:00  === daily starting ===",
                         f"{d0 + timedelta(days=1)} 08:00:00  === daily finished, exit 0 ==="])
    runs = health_report.morning_runs(tmp_path, d0)
    assert runs[str(d0)]["stopped"] and runs[str(d0)]["hours"] == 3.5 and "unfinished" not in runs[str(d0)]
    found = health_report.problems({"runs": runs, "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100})
    assert not any(str(d0) in p for p in found), found


def base_week(**kw):
    return {"runs": {}, "scrapes": [], "failing": [], "backup": {"hours": 2}, "free_gb": 100, **kw}


def test_a_day_that_read_few_boards_is_not_judged_yet():
    # Made mid-run on 18 Oct 2026, the report called 15 failures in the first 135 boards a problem.
    early = {"day": "2026-10-18", "boards": 135, "failed": 15, "new": 3, "sources": {"bamboohr": 15}}
    assert health_report.problems(base_week(boards=25000, scrapes=[early])) == []
    full = {**early, "boards": 21835, "failed": 1199, "sources": {"bamboohr": 900, "workable": 250, "lever": 40}}
    assert health_report.problems(base_week(boards=25000, scrapes=[full])) == [
        "1199 of 21835 board scrapes failed on 2026-10-18 (bamboohr 900, workable 250, lever 40)."]
    small = {**early, "boards": 120, "failed": 30}             # a whole day on a small install is judged
    assert health_report.problems(base_week(boards=120, scrapes=[small]))[0].startswith("30 of 120 board scrapes")


def test_every_failing_board_is_counted_and_refusals_are_apart():
    # Every weekly report said "15 board(s)": the query stopped at the 15 it lists. 18 Oct had 87.
    listed = [{"source": "greenhouse", "board": f"b{i}", "failures": 5, "error": "FetchError"} for i in range(15)]
    found = health_report.problems(base_week(failing=listed, failing_count=87,
                                             failing_sources={"greenhouse": 50, "lever": 30, "ashby": 7},
                                             refused=[{"source": "workable", "refused": 2785, "reads": 3625}]))
    assert found[0].startswith("87 board(s) failed 3+ times this week (greenhouse 50, lever 30, ashby 7).")
    assert found[1] == ("Workable turned us away for asking too often on 2785 of 3625 board reads this week, "
                        "so those boards were not read.")
