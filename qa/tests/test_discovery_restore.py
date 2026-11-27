"""With no saved database in the cache, it is restored from the newest verified result (26 Nov 2026)."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from devops import discovery_merge as dm
from devops import discovery_restore as rs
from devops import discovery_result as dr
from qa.tests.test_discovery_partitions import (  # noqa: F401 -- `offline`: the autouse no-network fixture
    BASE_BOARDS, healthy_world, joined, offline, plan_for, run_all, seed)

PASS = "pass-for-test"
NOW = datetime.now(timezone.utc)


def locked_result(tmp_path, run_id="700", finished=None, tamper=None):
    base = seed(tmp_path / f"base-{run_id}.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path / f"m{run_id}", base, plan)
    j = joined(tmp_path, base, f"join-{run_id}.db")
    cov = dm.merge(j, plan, parts)
    result = dr.build(j, plan, cov["outcomes"], dm.public(cov), PASS, run_id=run_id, attempt="1",
                      started=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), now=finished or NOW)
    if tamper:
        tamper(result)
    folder = tmp_path / f"art-{run_id}"
    folder.mkdir()
    raw = folder / "r.tar"
    with tarfile.open(raw, "w") as tar:
        for name, data in (("result.json", json.dumps(result).encode()), ("jobs.db", j.read_bytes())):
            info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt",
                    "-in", str(raw), "-out", str(folder / rs.LOCKED), "-pass", f"pass:{PASS}"], check=True)
    raw.unlink()
    return folder, j


def fake(monkeypatch, folders):
    monkeypatch.setattr(rs, "candidates", lambda repo, now: [{"id": k, "created": NOW} for k in folders])
    import shutil
    return lambda run_id, into: shutil.copy2(folders[run_id] / rs.LOCKED, into / rs.LOCKED)


def test_an_empty_cache_is_restored_from_the_newest_verified_result(tmp_path, monkeypatch):
    folder, db = locked_result(tmp_path)
    dest = tmp_path / "ck" / "data" / "jobs.db"
    got = rs.restore(dest, "o/r", PASS, download=fake(monkeypatch, {"700": folder}))
    assert got["restored_from"] == "700" and dest.read_bytes() == db.read_bytes()


def test_a_tampered_or_foreign_result_is_skipped_for_the_next_and_none_is_a_cold_start(tmp_path, monkeypatch):
    bad, _ = locked_result(tmp_path, "701", tamper=lambda r: r["run"].update(attempt="9"))     # signature breaks
    good, db = locked_result(tmp_path, "700")
    dest = tmp_path / "ck" / "jobs.db"
    got = rs.restore(dest, "o/r", PASS, download=fake(monkeypatch, {"701": bad, "700": good}))
    assert got["restored_from"] == "700" and dest.read_bytes() == db.read_bytes()
    dest2 = tmp_path / "ck2" / "jobs.db"
    got = rs.restore(dest2, "o/r", "another passphrase", download=fake(monkeypatch, {"700": good}))
    assert got["restored_from"] is None and not dest2.exists()


def test_a_result_older_than_six_days_is_not_used(tmp_path, monkeypatch):
    old, _ = locked_result(tmp_path, "690", finished=NOW - timedelta(days=7))
    dest = tmp_path / "ck" / "jobs.db"
    got = rs.restore(dest, "o/r", PASS, download=fake(monkeypatch, {"690": old}))
    assert got["restored_from"] is None and "too old" not in str(dest)


def test_a_saved_database_already_there_is_never_replaced(tmp_path, monkeypatch):
    dest = tmp_path / "jobs.db"
    dest.write_bytes(b"mine")
    assert rs.restore(dest, "o/r", PASS)["restored_from"] is None and dest.read_bytes() == b"mine"


def test_the_merge_says_where_its_history_came_from(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    out = tmp_path / "cov.json"
    import sys
    from qa.tests.test_discovery_partitions import ROOT
    j = joined(tmp_path, base)
    pd = tmp_path / "parts"
    for pid, p in parts.items():
        (pd / f"part-{pid}").mkdir(parents=True)
        import shutil
        shutil.copy2(p, pd / f"part-{pid}" / "part.db")
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps(plan))
    subprocess.run([sys.executable, "-m", "devops.discovery_merge", "--db", str(j), "--manifest", str(m),
                    "--parts-dir", str(pd), "--coverage", str(out), "--restored-from", "700"], cwd=ROOT,
                   capture_output=True, text=True)
    cov = json.loads(out.read_text())
    assert cov["restored_from"] == "700" and cov["cold_start"] is False
    from devops import discovery_outcome as do
    assert "restored" in do.summary(do.COMPLETE, [], [], cov)
