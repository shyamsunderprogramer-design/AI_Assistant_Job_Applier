"""A press the Auto-apply page draws again under still counts (26 Oct 2026). The page wrote its buttons'
words and its whole list of chosen jobs again at every step a run made. WebKit (Safari) drops a click
when the pressed button's words are written again between press and release, and both browsers drop it
when the button itself is drawn again: "Prepare these" and a row's Cancel were pressed and nothing
happened, and later checks reproduced the lost-click defect. The original three interrupted runs lost
their diagnostics, so their individual causes are not established. The app here runs on a free port of this Mac with
a database of made-up jobs, and nothing it is asked to start is started."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from qa.browser_checks import browser_diagnostics
from backend.apply import execution, runner
from backend.apply.execution import CANCELLED
from data_engineering.db import session as session_mod
from data_engineering.db.models import ApplyItem, Job
from data_engineering.db.session import get_session, init_engine

NOW = datetime.now(timezone.utc)
JOBS = {701: "Example Robotics", 702: "Sample Analytics", 703: "Demo Logistics"}


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Person:
    max_per_day = 10
    stop_before_submit = True

    def __init__(self):
        self.authorisation, self.answers, self.job, self.job_answers = {}, {}, None, {}


@pytest.fixture
def app_copy(tmp_path, monkeypatch):
    from werkzeug.serving import make_server

    from backend.api import app as webapp
    from data_engineering.scraper.filters import JobFilter
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with get_session() as s:
        for n, company in JOBS.items():
            s.add(Job(id=n, source="greenhouse", company=company, company_slug=company.lower().replace(" ", "-"),
                      external_id=str(n), title="Platform Engineer", location="Remote", description="x " * 400,
                      requirements="", application_url=f"https://example.com/{n}", content_hash=f"h{n}",
                      is_open=True, ats_match_score=0.9, score_basis="full", status="Not Applied",
                      posted_at=NOW - timedelta(days=2)))
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter())
    monkeypatch.setattr(runner, "load", lambda path: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    monkeypatch.setenv("RESUME_PROVIDER", "ollama")                   # a free writer: Prepare these asks nothing
    cfg = Cfg({"limits.max_applications_per_day": 15})
    monkeypatch.setattr(webapp, "cfg", lambda: cfg)
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: cfg)  # its own name for it too
    started = []

    def start_task(task, args):
        started.append((task, args))
        return webapp.jsonify(ok=True)
    monkeypatch.setattr(webapp, "start_task", start_task)
    monkeypatch.setattr("backend.api.chosen_pages.start_task", start_task)
    monkeypatch.setattr(webapp, "task_pid", lambda task: None)
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: None)
    stream = execution.stream                                          # a second's stream, then the page comes back
    monkeypatch.setattr(execution, "stream", lambda after, **kw: stream(after, seconds=1, every=0.1))
    execution.add(list(JOBS))
    server = make_server("127.0.0.1", 0, webapp.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", started
    server.shutdown()
    time.sleep(1.2)                                                    # the last stream ends before its database goes
    session_mod._engine, session_mod._SessionFactory = before


def press_while_drawn_again(page, selector, draw):
    """Press the button, let the page draw again, release: a step of a run arriving mid-click."""
    page.locator(selector).scroll_into_view_if_needed()
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.evaluate(draw)
    page.mouse.up()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("width", [1440, 390])
def test_a_press_the_page_draws_again_under_still_counts(app_copy, engine, width, tmp_path):
    base, started = app_copy
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        browser = getattr(manager, engine).launch()
    except Exception as exc:                       # no such browser installed here
        manager.stop()
        pytest.skip(f"no {engine}: {exc}")
    try:
        page = browser.new_page(viewport={"width": width, "height": 900 if width > 500 else 844},
                                service_workers="block", reduced_motion="reduce")
        with browser_diagnostics(page, tmp_path, f"press-{engine}-{width}"):
            page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/apply", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelectorAll('#chosen-list tr[data-row]').length === 3")
            page.wait_for_function("document.getElementById('chosen-prepare').textContent === 'Prepare these (3)'")
            first, second = page.evaluate("() => CH.order.slice(0, 2)")

            # A row's Cancel, while another job's row is drawn again.
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/apply/chosen/cancel"),
                                      timeout=5000):
                press_while_drawn_again(page, f'#chosen-list .ch-acts-{"wide" if width > 500 else "narrow"} [data-ch="cancel"][data-item="{first}"]',
                                        f"() => {{ const o = CH.items.get({second}); o.updated += ' '; draw(); }}")
            with get_session() as s:
                assert [s.get(ApplyItem, i).state == CANCELLED for i in (first, second)] == [True, False]

            # The card's own "Prepare these", while the whole card is drawn again, as at every step.
            page.wait_for_function("document.getElementById('chosen-prepare').textContent === 'Prepare these (2)'")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/apply/chosen/prepare"),
                                      timeout=5000):
                press_while_drawn_again(page, "#chosen-prepare", "() => draw()")
            assert started == [("prepare-chosen", ["prepare-apply", "--chosen"])]   # asked for once; nothing else
            assert not errors
    finally:
        browser.close()
        manager.stop()
