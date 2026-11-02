"""Status changes made in the Excel sheet, read back into the app (Phase 9, 31 Oct 2026): only a cell
the person changed since the last read, only a person's own status, an application only ever moving
on, the app's status standing where both changed, one history line, and the workbook never written.
A temporary database, made-up jobs and a workbook in a temporary folder."""

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from backend.apply import runner
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine
from data_engineering.excel import readback
from data_engineering.excel.readback import (APPS_OWN, BOTH, MOVED, NOT_A_STATUS, REFUSED, SAME, TAKE, TWO_ROWS,
                                             UNCHANGED, read_back)
from data_engineering.excel.tracker import ExcelTracker

NOW = datetime.now(timezone.utc)


@pytest.fixture
def book(tmp_path):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with get_session() as s:
        for n in range(1, 7):
            s.add(Job(id=n, source="greenhouse", company=f"Example Co {n}", company_slug=f"c{n}", external_id=str(n),
                      title="Platform Engineer", location="Remote", description="x " * 40, requirements="",
                      application_url=f"https://example.com/{n}", content_hash=f"h{n}", is_open=True,
                      ats_match_score=0.9, score_basis="full", status="Not Applied", posted_at=NOW - timedelta(days=2)))
    yield tmp_path / "tracker.xlsx"
    session_mod._engine, session_mod._SessionFactory = before


def export(book):
    with get_session() as s:
        ExcelTracker(book).export(s.query(Job).order_by(Job.id).all())


def rows(ws):
    head = {c.value: c.column for c in ws[1]}
    return head, [r for r in range(2, ws.max_row + 1) if ws.cell(r, head["Job Key"]).value]


def edit(book, **statuses):
    """The person's edits: job_1="Applied" sets job 1's Application Status cell."""
    wb = load_workbook(book)
    ws = wb["Jobs"]
    head, found = rows(ws)
    for r in found:
        job_id = int(ws.cell(r, head["Job Key"]).value.rsplit(":", 1)[1])
        if f"job_{job_id}" in statuses:
            ws.cell(r, head["Application Status"]).value = statuses[f"job_{job_id}"]
    wb.save(book)


def set_app(**statuses):
    with get_session() as s:
        for name, value in statuses.items():
            job = s.get(Job, int(name.split("_")[1]))
            job.status, job.exported_to_excel = value, False


def status(job_id):
    with get_session() as s:
        return s.get(Job, job_id).status


def verdicts(changes):
    return {c.job_id: c.verdict for c in changes}


def lines():
    return [json.loads(l) for l in runner.AUDIT_LOG.read_text().splitlines()] if runner.AUDIT_LOG.exists() else []


def test_a_change_made_in_the_sheet_comes_in_once_with_one_history_line_and_the_sheet_untouched(book):
    export(book)
    edit(book, job_1="Applied")
    sheet = book.read_bytes()
    assert verdicts(read_back(book))[1] == TAKE                     # the preview writes nothing at all
    assert status(1) == "Not Applied" and not readback.seen_path(book).exists() and not lines()

    assert verdicts(read_back(book, apply=True))[1] == TAKE
    assert status(1) == "Applied" and book.read_bytes() == sheet   # the workbook is never written
    assert [(l["job_id"], l["how"], l["stage"], l["marked_on"]) for l in lines()] == [
        (1, "marked", "Applied", "the Excel sheet")]
    with get_session() as s:
        assert s.get(Job, 1).applied_at is None                    # when it went out: not known

    assert verdicts(read_back(book, apply=True))[1] == SAME        # read again: nothing twice
    export(book)
    assert verdicts(read_back(book, apply=True))[1] == SAME and len(lines()) == 1


def test_a_status_the_export_wrote_itself_is_never_taken_for_the_persons_edit(book):
    export(book)
    read_back(book, apply=True)                                    # the first read: what each row showed
    set_app(job_2="Skipped")
    export(book)                                                   # the sheet shows Skipped, written by the app
    set_app(job_2="Not Applied")                                   # un-skipped in the app
    export(book)                                                   # the export never overwrites Skipped
    assert verdicts(read_back(book, apply=True))[2] == UNCHANGED and status(2) == "Not Applied"


def test_a_job_changed_in_both_places_keeps_the_apps_status(book):
    export(book)
    read_back(book, apply=True)
    edit(book, job_3="Applied")
    set_app(job_3="Skipped")
    changes = read_back(book, apply=True)
    assert verdicts(changes)[3] == BOTH and status(3) == "Skipped" and not lines()
    assert "changed in both places" in next(c.why for c in changes if c.job_id == 3)


def test_an_application_only_moves_on_and_the_apps_own_statuses_never_come_in(book):
    set_app(job_1="Applied", job_2="Interviewing", job_3="Offer", job_5="Applied")
    export(book)
    assert set(verdicts(read_back(book, apply=True)).values()) == {SAME}
    edit(book, job_1="Skipped", job_2="Applied", job_3="Rejected", job_4="Closed", job_5="interviewing",
         job_6="Apllied")
    changes = read_back(book, apply=True)
    why = {c.job_id: c.why for c in changes}
    assert verdicts(changes) == {1: REFUSED, 2: REFUSED, 3: REFUSED, 4: APPS_OWN, 5: TAKE, 6: NOT_A_STATUS}
    assert why[1] == "an application stays an application" and why[2] == "a stage never goes back"
    assert why[3] == "Offer and Rejected are final"
    assert [status(n) for n in range(1, 7)] == [
        "Applied", "Interviewing", "Offer", "Not Applied", "Interviewing", "Not Applied"]
    assert [l["job_id"] for l in lines()] == [5]                    # moved on by hand, with no record yet


def test_a_first_read_takes_the_sheet_only_over_a_status_the_app_set_by_itself(book):
    export(book)
    set_app(job_2="Applied")                                       # not exported since
    runner.AUDIT_LOG.write_text(json.dumps({"job_id": 3, "how": "marked"}) + "\n")   # on record, then set back
    edit(book, job_1="Skipped", job_2="Interviewing", job_3="Applied")
    changes = read_back(book, apply=True)
    assert verdicts(changes)[1] == TAKE and verdicts(changes)[2] == BOTH and verdicts(changes)[3] == BOTH
    assert [status(n) for n in (1, 2, 3)] == ["Skipped", "Applied", "Not Applied"]
    assert "on record as applied" in next(c.why for c in changes if c.job_id == 3)


def test_two_rows_for_one_job_that_disagree_change_nothing(book):
    export(book)
    wb = load_workbook(book)
    ws = wb["Jobs"]
    head, found = rows(ws)
    copy = ws.max_row + 1
    for col in range(1, ws.max_column + 1):
        ws.cell(copy, col).value = ws.cell(found[0], col).value
    ws.cell(found[0], head["Application Status"]).value = "Applied"
    ws.cell(copy, head["Application Status"]).value = "Skipped"
    wb.save(book)
    assert verdicts(read_back(book, apply=True))[1] == TWO_ROWS and status(1) == "Not Applied"


def test_a_status_changed_in_the_app_while_reading_keeps_the_apps(book, monkeypatch):
    export(book)
    edit(book, job_1="Applied")
    real = readback.plan

    def plan_then_the_app_moves(path):
        found = real(path)
        set_app(job_1="Skipped")                                   # e.g. the person, on the job page
        return found
    monkeypatch.setattr(readback, "plan", plan_then_the_app_moves)
    assert verdicts(read_back(book, apply=True))[1] == MOVED and status(1) == "Skipped" and not lines()


def test_a_sheet_it_cannot_read_changes_nothing(book):
    with pytest.raises(readback.Unreadable, match="run `main.py export` first"):
        read_back(book, apply=True)
    export(book)
    wb = load_workbook(book)
    ws = wb["Jobs"]
    head, _ = rows(ws)
    ws.cell(1, head["Job Key"]).value = "Something else"
    wb.save(book)
    with pytest.raises(readback.Unreadable, match="no Job Key or Application Status column"):
        read_back(book, apply=True)
    assert not readback.seen_path(book).exists()


def test_the_command_previews_then_takes_in(book, capsys):
    import main

    class Cfg:
        database_url = f"sqlite:///{book.parent / 'jobs.db'}"

        def get(self, key, default=None):
            return str(book) if key == "excel.path" else default
    export(book)
    edit(book, job_4="Skipped")
    assert main.cmd_readback(Cfg(), argparse.Namespace(apply=False)) == 0
    assert "To take in: 1" in capsys.readouterr().out and status(4) == "Not Applied"
    assert main.cmd_readback(Cfg(), argparse.Namespace(apply=True)) == 0
    out = capsys.readouterr().out
    assert "Taken in: 1" in out and "Example Co 4 — Platform Engineer: Not Applied → Skipped" in out
    assert status(4) == "Skipped"
