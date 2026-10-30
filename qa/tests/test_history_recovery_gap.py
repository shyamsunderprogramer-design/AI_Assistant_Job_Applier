"""Acceptance regressions: a committed application must survive a failed history write.

These reproduced the database/JSONL crash window before the approved outbox
repair. All records here are fictional, in temporary storage.
"""
import pytest

from backend.apply import execution, runner
from data_engineering.db.models import Job
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import db, item_of  # noqa: F401


def cannot_write(*args, **kwargs):
    raise OSError("simulated full disk after database commit")


@pytest.mark.parametrize("path", ["runner", "chosen_confirmation"])
def test_committed_application_history_is_recovered_after_log_write_failure(db, monkeypatch, path):
    entry = {"company": "Globex", "title": "Platform Engineer", "how": "you"}
    if path == "chosen_confirmation":
        execution.add([1])
        item = item_of(1)
        for state in (execution.VALIDATING, execution.PREPARING, execution.READY,
                      execution.RUNNING, execution.PENDING):
            execution.move(item.id, state)
    with monkeypatch.context() as patch:
        patch.setattr(runner, "write_audit", cannot_write)
        with pytest.raises(OSError, match="simulated full disk"):
            if path == "runner":
                runner.record_application(1, entry)
            else:
                execution.confirm(item.id, went=True)
    with get_session() as session:
        job = session.get(Job, 1)
        assert job.status == "Applied" and job.applied_at is not None
        first = job.applied_at
    execution.recover()
    # A retry must never submit again or duplicate the application. Recovery
    # must retain the original history payload and export its one missing line.
    assert runner.record_application(1, entry) is False
    with get_session() as session:
        assert session.get(Job, 1).applied_at == first
    assert runner.AUDIT_LOG.exists(), "committed application permanently lost its history record"
    assert len(runner.AUDIT_LOG.read_text().splitlines()) == 1
