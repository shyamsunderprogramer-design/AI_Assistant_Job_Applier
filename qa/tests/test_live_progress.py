"""Auto-apply Phase 2 (25 Oct 2026): the chosen jobs followed as they happen. Each step is sent to the
page as it is written (server-sent events); a page that reconnects is sent only what it missed; every
entry carries the id of its latest step, so an older picture never wins; a run that died is set right
while the page watches; and nothing is ever shown Applied on a form's own words. A temporary database
and made-up jobs: no browser, model or network (conftest refuses them)."""

import json
import os
import threading
import time

from backend.api import app as webapp
from backend.apply import execution, runner
from backend.apply.execution import APPLIED, PENDING, PREPARING, QUEUED, READY, RUNNING, VALIDATING
from data_engineering.db.models import ApplyEvent, ApplyRun
from data_engineering.db.session import get_session, init_engine
from qa.tests.test_execution_queue import (NOW, Context, client, db, dead_pid, documents,  # noqa: F401
                                           fake_form, item_of)


def messages(body: str) -> list[dict]:
    """The server-sent events in a body, each {event, id, data}; comments and the retry line left out."""
    out = []
    for block in body.split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if line and not line.startswith(":"):
                key, _, value = line.partition(": ")
                fields[key] = value
        if "event" in fields:
            out.append({"event": fields["event"], "id": int(fields["id"]) if "id" in fields else None,
                        "data": json.loads(fields["data"])})
    return out


def read(c, path, headers=None):
    return messages(c.get(path, headers=headers or {}).get_data(as_text=True))


def stream(after=0, **kwargs):
    return messages("".join(execution.stream(after, **{"seconds": 1, "every": 0.05, **kwargs})))


def activity(got):
    return [m for m in got if m["event"] == "activity"]


def all_event_ids():
    with get_session() as s:
        return [e.id for e in s.query(ApplyEvent).order_by(ApplyEvent.id)]


# -- live ----------------------------------------------------------------------------------------

def test_every_step_is_sent_as_it_is_written_in_order_and_once(client):
    c, _ = client
    execution.add([1, 2])
    a, b = item_of(1).id, item_of(2).id

    def a_run_going():
        time.sleep(0.15)
        execution.move(a, VALIDATING)
        execution.note(a, "Checking it can still go")
        execution.move(a, PREPARING)
        time.sleep(0.1)
        execution.move(b, VALIDATING)
    writer = threading.Thread(target=a_run_going)
    writer.start()
    got = read(c, "/apply/chosen/stream?after=0&seconds=1&every_ms=50")
    writer.join()
    ids = [m["id"] for m in activity(got)]
    assert ids == all_event_ids()                                   # every one, in order, none twice
    assert {m["data"]["kind"] for m in activity(got)} == {"state", "step"}
    steps = [m["data"] for m in activity(got) if m["data"]["kind"] == "step"]
    assert steps[0]["message"] == "Checking it can still go" and steps[0]["job"] == "Globex — Platform Engineer"
    assert steps[0]["clock"].endswith(("AM", "PM")) and steps[0]["at"].endswith("+00:00")
    latest = {m["data"]["id"]: m["data"] for m in got if m["event"] == "item"}
    assert latest[a]["state"] == PREPARING and latest[b]["state"] == VALIDATING
    assert latest[a]["version"] == max(m["id"] for m in activity(got) if m["data"]["item_id"] == a)
    assert latest[a]["current"].startswith("Preparing") and latest[a]["current_at"]
    assert [m["data"]["live"] for m in got if m["event"] == "run"] == [False]     # sent once, unchanged


def test_a_page_that_reconnects_is_sent_only_what_it_missed(client):
    c, _ = client
    execution.add([1])
    a = item_of(1).id
    first = read(c, "/apply/chosen/stream?after=0&seconds=1&every_ms=50")
    seen = activity(first)[-1]["id"]
    execution.move(a, VALIDATING)
    execution.move(a, PREPARING)
    # The browser comes back by itself with the last id it had; its first URL still says after=0.
    again = read(c, "/apply/chosen/stream?after=0&seconds=1&every_ms=50", headers={"Last-Event-ID": str(seen)})
    missed = [m["id"] for m in activity(again)]
    assert len(missed) == 2 and min(missed) > seen
    resumed = read(c, f"/apply/chosen/stream?after={missed[0]}&seconds=1&every_ms=50")   # a page's own `after`
    assert [m["id"] for m in activity(resumed)] == missed[1:]


def test_each_entry_carries_its_latest_steps_id_so_an_older_picture_never_wins(client):
    c, _ = client
    execution.add([1])
    a = item_of(1).id
    older = c.get("/apply/chosen").get_json()["items"][0]
    run = execution.start_run(os.getpid())          # a live run: the full read's recovery leaves its job be
    execution.move(a, VALIDATING, run_id=run.id)
    execution.note(a, "Checking it can still go")
    newer = c.get("/apply/chosen").get_json()
    item = newer["items"][0]
    assert item["version"] > older["version"] and item["version"] == newer["last_event_id"]
    assert (item["current"], bool(item["current_at"])) == ("Checking it can still go", True)
    assert older["current"] == ""                                      # queued: no step under way
    # Where the full read ends the stream takes over: nothing missed, nothing twice.
    tail = read(c, f"/apply/chosen/stream?after={newer['last_event_id']}&seconds=1&every_ms=50")
    assert activity(tail) == []


def test_two_pages_and_three_writers_at_once_each_page_gets_every_step_once(client):
    execution.add([1, 2, 5])
    items = [item_of(j).id for j in (1, 2, 5)]
    pages = {}

    def page(name):
        pages[name] = read(webapp.app.test_client(), "/apply/chosen/stream?after=0&seconds=2&every_ms=50")

    def writer(item_id):
        for n in range(8):
            execution.note(item_id, f"step {n}")
            time.sleep(0.01)
    threads = [threading.Thread(target=page, args=(n,)) for n in (1, 2)]
    threads += [threading.Thread(target=writer, args=(i,)) for i in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    every = all_event_ids()
    assert len(every) == 3 + 24
    for name in (1, 2):
        assert [m["id"] for m in activity(pages[name])] == every, name


def test_the_resume_being_made_ready_is_followed_step_by_step(db, monkeypatch):
    """A real run writes its tailoring's progress while the job is Preparing; until 26 Oct 2026 those
    lines reached only the run's log, and minutes of tailoring showed no step on the page."""
    cfg, _, folder = db

    def prepare(cfg, job, applicant, head, *, tailor):
        runner.say(f"{head}: tailoring the resume (round 1 of 3)")
        runner.say(f"{head}: writing the cover letter")
        (folder / "resume.pdf").write_bytes(b"%PDF-1.4 made-up resume")
        return runner.Prepared(folder / "resume.pdf", None, "tailored", folder)
    monkeypatch.setattr(runner, "prepare_documents", prepare)
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted"}))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    steps = execution.events_after(0, item_id=item_of(1).id)
    words = [e["message"] for e in steps]
    at = words.index("tailoring the resume (round 1 of 3)")              # the job's name not repeated
    assert words[at + 1] == "writing the cover letter" and steps[at]["kind"] == "step"
    assert [e["state"] for e in steps[:at] if e["kind"] == "state"][-1] == PREPARING   # while it was preparing
    assert steps[-1]["state"] == APPLIED                                  # the fake form's confirmation


# -- a run that died, and what may have gone -------------------------------------------------------

def test_a_run_that_died_is_set_right_while_the_page_watches(db):
    execution.add([1, 2])
    a, b = item_of(1).id, item_of(2).id
    with get_session() as s:
        run = ApplyRun(state="running", pid=dead_pid(), started_at=NOW.replace(tzinfo=None))
        s.add(run)
        s.flush()
        run_id = run.id
    for state in (VALIDATING, PREPARING, READY, RUNNING):
        execution.move(a, state, run_id=run_id)
    execution.move(b, VALIDATING, run_id=run_id)
    got = stream(check_alive=0.1)
    runs = [m["data"] for m in got if m["event"] == "run"]
    assert runs[0]["live"] is True and runs[-1]["live"] is False and runs[-1]["last"]["state"] == "interrupted"
    latest = {m["data"]["id"]: m["data"] for m in got if m["event"] == "item"}
    assert latest[a]["state"] == PENDING and "may have been sent" in latest[a]["action"]
    assert latest[b]["state"] == QUEUED
    assert APPLIED not in {m["data"]["state"] for m in got if m["event"] in ("activity", "item")}


def test_a_job_is_never_shown_applied_on_its_forms_own_words(db, documents, monkeypatch):
    cfg, _, _ = db

    def thanks_on_screen(job):
        runner.say("   Thank you for applying! Application submitted ✓")      # the form's words, not a check
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "timeout"}, before=thanks_on_screen))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    got = stream()
    shown = [m["data"]["state"] for m in got if m["event"] in ("activity", "item") and m["data"]["state"]]
    assert APPLIED not in shown and shown[-1] == PENDING
    assert any("Thank you for applying" in m["data"]["message"] for m in activity(got))   # seen, as a step
    execution.confirm(item_of(1).id, went=True)                                 # the person's word
    after = stream(activity(got)[-1]["id"])
    assert [m["data"]["state"] for m in after if m["event"] == "item"] == [APPLIED]
    assert activity(after)[-1]["data"]["message"].startswith("Applied: you said it went")


# -- kept, and the page's own routes ---------------------------------------------------------------

def test_the_history_and_the_stream_survive_the_app_starting_again(db):
    _, url, _ = db
    execution.add([1])
    a = item_of(1).id
    execution.move(a, VALIDATING)
    execution.note(a, "Checking it can still go")
    before = execution.events_after(0)
    init_engine(url)                                                          # the app starts again
    assert execution.events_after(0) == before
    execution.move(a, PREPARING)
    got = stream(before[-1]["id"])
    assert [m["data"]["state"] for m in activity(got)] == [PREPARING]


def test_a_quiet_stream_still_says_it_is_there(db):
    body = "".join(execution.stream(0, seconds=1, every=0.05, heartbeat=0.2))
    assert body.startswith("retry: 2000") and ": still here" in body


def test_a_jobs_timeline_and_catching_up_without_the_stream(client):
    c, _ = client
    execution.add([1, 2])
    a = item_of(1).id
    execution.move(a, VALIDATING)
    timeline = c.get(f"/apply/chosen/events?item={a}").get_json()
    assert [e["state"] for e in timeline["events"]] == [QUEUED, VALIDATING]
    assert {e["item_id"] for e in timeline["events"]} == {a}
    assert c.get("/apply/chosen/events?item=abc").status_code == 400
    caught = c.get("/apply/chosen/events?after=1").get_json()
    assert [e["id"] for e in caught["events"]] == all_event_ids()[1:] and caught["last_event_id"] == all_event_ids()[-1]
    # Nonsense where a step's number should be: from the start, not an error.
    junk = read(c, "/apply/chosen/stream?after=-5&seconds=1&every_ms=50", headers={"Last-Event-ID": "nonsense"})
    assert [m["id"] for m in activity(junk)] == all_event_ids()
