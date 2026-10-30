"""Legacy entry points remain manual even when the saved preference permits auto-submit.

Fictional jobs, temporary documents/database and fake employer forms only.
The chosen-jobs automatic path remains covered by test_other_boards and Phase 3.
"""
import pytest

from backend.apply import anyform, greenhouse, prepare, runner
from data_engineering.db.models import Job
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import db  # noqa: F401
from qa.tests.test_prepared_documents import Browser, dana, desk  # noqa: F401


@pytest.mark.parametrize("entry", ["next", "prepared", "job", "assistant_one", "assistant_count"])
def test_legacy_entry_points_never_press_submit(db, desk, monkeypatch, entry):
    cfg, _, _ = db
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("timeout", {}))
    # Only a Greenhouse job is available, with a fully answered mock form.
    with get_session() as s:
        for job in s.query(Job):
            job.is_open = job.id == 6
    monkeypatch.setattr(prepare, "pending", lambda: [{"job_id": 6}])
    args = {"prepared": True} if entry == "prepared" else {"job_id": 6} if entry in ("job", "assistant_one") else {"limit": 10}
    outcomes = runner.run(cfg, browser_factory=Browser, wait_minutes=0, **args)
    assert len(Browser.forms) == len(outcomes) == 1
    assert not Browser.forms[0].clicked
    assert outcomes[0].result == "timeout" and outcomes[0].how == "you"
    assert not runner.AUDIT_LOG.exists()
    with get_session() as s:
        assert s.get(Job, 6).status == "Not Applied" and s.get(Job, 6).applied_at is None


def test_direct_legacy_helper_ignores_auto_true(db, desk, monkeypatch):
    cfg, _, _ = db
    seen = []
    monkeypatch.setattr(runner, "fill_form", lambda *a, **kw: seen.append(kw["auto"]))
    with get_session() as s:
        job = s.get(Job, 6)
        runner.apply_one(cfg, object(), job, dana(stop_before_submit=False), "mock", auto=True, tailor=False, wait_s=0)
    assert seen == [False]


def test_cross_board_redirect_ignores_auto_true_even_when_called_directly(db, desk, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setattr(anyform, "reach", lambda page, *a, **kw:
                        anyform.Reached(page, "greenhouse", "https://boards.greenhouse.io/example/jobs/3"))
    seen = []
    monkeypatch.setattr(runner, "_apply_greenhouse", lambda *a, **kw: seen.append(kw["auto"]))
    with get_session() as s:
        job = s.get(Job, 3)
        ready = desk.prepare_documents(cfg, job, dana(), "mock", tailor=False)
        runner.fill_form(cfg, object(), job, dana(stop_before_submit=False), "mock", ready, auto=True, wait_s=0)
    assert seen == [False]


def test_legacy_preview_reports_effective_manual_mode(db, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    assert runner.next_up(cfg)["auto_submit"] is False
