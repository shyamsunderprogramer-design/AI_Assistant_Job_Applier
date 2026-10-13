"""Every tailored version: listed, compared, and deleted unless a job attaches it."""

import json

import pytest

from ml.tailoring import history, pipeline


@pytest.fixture
def runs(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(history, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(history, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(history, "_jobs", lambda ids: {7: {"title": "SRE", "company": "Acme"}})
    for name, ats, job in (("20261001-100000-aaaaaa", [80, 90], 7), ("20261002-100000-bbbbbb", [80, 85], None)):
        f = tmp_path / "runs" / name
        f.mkdir(parents=True)
        (f / "result.json").write_text(json.dumps({"title": "", "before_after": {"ats": ats},
                                                   "pages": {"pdf": ["p1.png"]}, "checks_ok": True}))
        if job:
            (f / "job.json").write_text(json.dumps({"job_id": job}))
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "7.json").write_text(json.dumps({"best": "20261001-100000-aaaaaa"}))
    return tmp_path


def test_runs_are_listed_newest_first_with_their_job(runs):
    items = history.runs()
    assert [i["id"][:8] for i in items] == ["20261002", "20261001"]
    attached = items[1]
    assert attached["attached_to"] == 7 and attached["company"] == "Acme" and attached["title"] == "SRE"
    assert attached["ats"] == [80, 90] and attached["pages"] == 1


def test_an_attached_run_is_not_deleted(runs):
    with pytest.raises(PermissionError):
        history.delete("20261001-100000-aaaaaa")
    history.delete("20261002-100000-bbbbbb")
    assert [i["id"] for i in history.runs()] == ["20261001-100000-aaaaaa"]


def test_compare_lines_up_the_two(monkeypatch, runs):
    lines = {"a": ["Summary", "Built X.", "Old line."], "b": ["Summary", "Built X better.", "New line.", "Extra."]}
    monkeypatch.setattr(history, "lines_of", lambda run: (lines[run], False))
    out = history.compare("a", "b")
    assert out["same"] == 1 and out["changed"] == 2 and out["only_b"] == 1 and not out["rough"]
