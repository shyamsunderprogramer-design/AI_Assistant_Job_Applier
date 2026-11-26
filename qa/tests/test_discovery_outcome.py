"""COMPLETE, PARTIAL or FAILED: the run's outcome from its steps and its coverage (26 Nov 2026; devops/discovery_outcome.py).

The coverage reports here are the real merge's (devops/discovery_merge.py) over the fake boards' world of
qa/tests/test_discovery_partitions.py; run #109's own counts are reproduced at the end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from devops import discovery_merge as dm
from devops import discovery_outcome as do
from qa.tests.test_discovery_partitions import (  # noqa: F401 -- `offline`: the autouse no-network fixture
    BASE_BOARDS, WORLD, healthy_world, joined, offline, opened, plan_for, run_all, seed)

OK = {"lock": "success", "build": "success", "upload": "success", "complete_upload": "skipped"}
OK_COMPLETE = {**OK, "complete_upload": "success"}


def merged(tmp_path, world=None, tamper=None, lose=()):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD.update(world or {})
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    for pid in parts:
        if any(pid.startswith(x) for x in lose):
            parts[pid] = None
    if tamper:
        tamper(parts)
    cov = dm.merge(joined(tmp_path, base), plan, parts)
    return cov, 0 if cov["complete_refresh"] else 3


def test_complete_discovery_is_complete_and_succeeds(tmp_path):
    cov, code = merged(tmp_path)
    assert code == 0
    assert do.decide(cov, code, OK_COMPLETE)[0] == do.COMPLETE


def test_a_valid_partial_discovery_is_partial_and_succeeds_with_its_unread_sites_named(tmp_path):
    cov, code = merged(tmp_path, {f"w{i}": 429 for i in range(1, 6)})          # Workday refuses
    status, failed, unread = do.decide(cov, code, OK)
    assert (status, failed) == (do.PARTIAL, []) and any(u.startswith("workday") for u in unread)
    assert "not a complete refresh" in do.summary(status, failed, unread, cov)


@pytest.mark.parametrize("case", ["all-refuse", "all-fail"])
def test_nothing_usable_when_every_provider_refuses_or_fails(tmp_path, case):
    said = 429 if case == "all-refuse" else 500
    world = {k: said for k in ("acme", "beta", "cyan", "dune", "w1", "w2", "w3", "w4", "w5")}
    cov, code = merged(tmp_path, world)
    status, failed, _ = do.decide(cov, code, OK)
    assert status == do.FAILED and any("no board was read" in f for f in failed)


def test_a_missing_partition_result_fails_the_run_though_the_others_are_published(tmp_path):
    cov, code = merged(tmp_path, lose=("workday",))
    status, failed, _ = do.decide(cov, code, OK)
    assert code == 3 and status == do.FAILED and any("partition" in f for f in failed)


def test_an_invalid_partition_result_fails_the_run(tmp_path):
    def corrupt(parts):
        pid = next(p for p in parts if p.startswith("greenhouse"))
        parts[pid].write_bytes(b"not a database")
    cov, code = merged(tmp_path, tamper=corrupt)
    assert do.decide(cov, code, OK)[0] == do.FAILED


@pytest.mark.parametrize("code", [4, 1, None])
def test_a_merge_that_stopped_or_never_ran_fails(tmp_path, code):
    cov, _ = merged(tmp_path, {"w1": 429})
    status, failed, _ = do.decide(cov, code, {})
    assert status == do.FAILED and any("merge" in f for f in failed)


@pytest.mark.parametrize("step,outcome", [("lock", "failure"), ("build", "failure"), ("upload", "failure"),
                                          ("build", "cancelled"), ("upload", "")])
def test_encryption_verification_or_publication_failing_fails_the_run(tmp_path, step, outcome):
    cov, code = merged(tmp_path, {"w1": 429})
    status, failed, _ = do.decide(cov, code, {**OK, step: outcome})
    assert status == do.FAILED and len(failed) == 1


def test_a_complete_run_whose_complete_copy_did_not_publish_fails(tmp_path):
    cov, code = merged(tmp_path)
    assert do.decide(cov, code, OK)[0] == do.FAILED                              # complete_upload skipped


def test_a_missing_or_inconsistent_coverage_report_fails(tmp_path):
    assert do.decide(None, 3, OK)[0] == do.FAILED
    cov, code = merged(tmp_path, {"w1": 429})
    assert do.decide(cov, 0, OK_COMPLETE)[0] == do.FAILED                       # exit says complete, report not
    cov["totals"]["reconciles"] = False
    assert do.decide(cov, code, OK)[0] == do.FAILED


def test_the_same_results_merged_twice_give_the_same_outcome(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD["w1"] = 429
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    j = joined(tmp_path, base)
    first = dm.merge(j, plan, parts)
    second = dm.merge(j, plan, parts)
    assert do.decide(first, 3, OK)[0] == do.decide(second, 3, OK)[0] == do.PARTIAL
    assert first["totals"]["boards_scanned_in_full"] == second["totals"]["boards_scanned_in_full"]


def run_main(tmp_path, monkeypatch, cov, code, **steps):
    path = tmp_path / "coverage.json"
    if cov is not None:
        path.write_text(json.dumps(dm.public(cov) if "outcomes" in cov else cov))
    out, page = tmp_path / "out.txt", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    args = ["--coverage", str(path), "--merge-code", "" if code is None else str(code)]
    for k, v in {**OK, **steps}.items():
        args += [f"--{k.replace('_', '-')}", v]
    rc = do.main(args + ["--slot", "2026-11-26 11:00 CST", "--trigger", "schedule"])
    return rc, out.read_text(), page.read_text()


def test_the_step_exits_zero_with_warnings_for_partial_and_one_for_failed(tmp_path, monkeypatch, capsys):
    cov, code = merged(tmp_path, {"w1": 429})
    rc, out, page = run_main(tmp_path, monkeypatch, cov, code)
    said = capsys.readouterr().out
    assert rc == 0 and "status=PARTIAL" in out and "::warning::Discovery PARTIAL" in said and "::error::" not in said
    assert "Discovery outcome: PARTIAL" in page and "complete refresh" in page
    rc, out, _ = run_main(tmp_path, monkeypatch, cov, code, upload="failure")
    said = capsys.readouterr().out
    assert rc == 1 and "status=FAILED" in out and "::error::Discovery FAILED: publishing" in said


def test_run_109_reproduced_partial_not_a_technical_failure():
    """Run #109 (26 Nov 2026, 11:00 window): every step succeeded but the old last step; 84.0% read in full."""
    cov = {"status": "PARTIAL", "partitions": [{"id": f"p{i}", "status": "PARTIAL", "reason": ""} for i in range(15)]
           + [{"id": "jazzhr-1of1", "status": "COMPLETE", "reason": ""}, {"id": "recruitee-1of1", "status": "COMPLETE", "reason": ""}],
           "providers": {"workday": {"boards": 2891, "counts": {"complete": 3, "rate_limited": 37}, "deferred": 2851}},
           "totals": {"boards_planned": 29082, "boards_scanned_in_full": 24437, "boards_read_in_part": 0,
                      "boards_failed": 141, "boards_rate_limited": 140, "boards_deferred_quota": 4364,
                      "coverage_pct": 84.0, "boards_needing_recovery": 4645, "reconciles": True}}
    status, failed, unread = do.decide(cov, 3, OK)
    assert status == do.PARTIAL and not failed and unread[0].startswith("workday: 3 of 2,891")
    page = do.summary(status, failed, unread, cov, slot="2026-11-26 11:00 CST", trigger="schedule",
                      now=datetime(2026, 11, 26, 17, 50, tzinfo=timezone.utc),
                      window_start=do._window_start("2026-11-26 11:00 CST"))
    assert "84.0%" in page and "4,645 boards" in page and "Deadline | met (due 19:00 UTC)" in page
    assert "COMPLETE —" not in page


def test_with_no_saved_database_at_all_the_merge_starts_from_the_results_instead_of_crashing(tmp_path):
    """26 Nov 2026 replay: the cache restored nothing, data/ did not exist, and the merge crashed (exit 1)."""
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    missing = tmp_path / "fresh-checkout" / "data" / "jobs.db"            # no folder, no file
    cov = dm.merge(missing, plan, parts)
    assert missing.is_file() and cov["status"] == "COMPLETE"
    assert do.decide(cov, 0, OK_COMPLETE)[0] == do.COMPLETE


def test_a_cold_start_is_said_and_a_saved_database_is_not_one(tmp_path, monkeypatch, capsys):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = run_all(tmp_path, base, plan)
    warm = dm.merge(joined(tmp_path, base), plan, parts)
    cold = dm.merge(tmp_path / "none" / "data" / "jobs.db", plan, parts)
    assert warm["cold_start"] is False and cold["cold_start"] is True
    assert "Cold start" in do.summary(do.COMPLETE, [], [], cold) and "Cold start" not in do.summary(do.COMPLETE, [], [], warm)
    run_main(tmp_path, monkeypatch, cold, 0, complete_upload="success")
    assert "::warning::Cold start" in capsys.readouterr().out


def test_with_no_saved_database_a_site_that_is_probed_while_it_refuses_is_probed(tmp_path, capsys):
    from devops import discovery_plan as dp
    csv = tmp_path / "boards.csv"
    csv.write_text("source,slug,name\n" + "".join(f"workday,w{i},W{i}\n" for i in range(300)) + "greenhouse,g1,G\n")
    out = tmp_path / "m.json"
    assert dp.main(["plan", "--db", str(tmp_path / "missing.db"), "--boards-csv", str(csv), "--out", str(out)]) == 0
    said = capsys.readouterr().out
    wd = [p for p in json.loads(out.read_text())["partitions"] if p["provider"] == "workday"]
    assert "cold_start=true" in said and wd and all(p["probe"] for p in wd)


def test_the_matrix_queues_the_longest_partitions_first():
    from devops import discovery_plan as dp
    manifest = {"partitions": [{"id": "a-1of1", "provider": "a", "boards": [], "workers": 1, "est_minutes": 2},
                               {"id": "z-1of1", "provider": "z", "boards": [], "workers": 1, "est_minutes": 20},
                               {"id": "m-1of1", "provider": "m", "boards": [], "workers": 1, "est_minutes": 9}]}
    assert [m["id"] for m in dp.matrix(manifest)] == ["z-1of1", "m-1of1", "a-1of1"]
