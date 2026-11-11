"""The morning's picks on the Auto-apply page and their settings on the Settings page (4 Nov 2026, step 2
of the Morning shortlist plan): what each pick shows, Remove / Not today / Restore acting on the morning's
picks only, the settings saved to their own file and checked, and the page's loading, empty, blocked and
error states in WebKit and Chromium, on a laptop and a phone. Made-up jobs, a database in a temporary
file, an app on a free port of this Mac; nothing is prepared, opened or sent."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import execution, runner, shortlist
from data_engineering.db import session as session_mod
from data_engineering.db.models import ApplyArtifact, ApplyEvent, ApplyItem
from data_engineering.db.session import get_session, init_engine
from qa.tests.test_morning_shortlist import ROWS, Cfg, Person, _job

ON = Cfg({"shortlist.enabled": True, "limits.max_applications_per_day": 15})
OFF = Cfg({"limits.max_applications_per_day": 15})


def now():
    return datetime.now(timezone.utc)


def _wire(monkeypatch, holder):
    """The app's settings are the test's: on the app and on each page module, which keep their own name."""
    from backend.api import app as webapp

    def config():
        return holder["cfg"]
    for target in ("backend.api.app.cfg", "backend.api.shortlist_pages.cfg", "backend.api.chosen_pages.cfg"):
        monkeypatch.setattr(target, config)
    return webapp


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with get_session() as s:
        for n, (company, board, fit, extra) in ROWS.items():
            s.add(_job(n, company, board, fit, extra))
    from data_engineering.scraper.filters import JobFilter
    from ml.resume.profile import NON_US_PLACES
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter(exclude_location_keywords=list(NON_US_PLACES)))
    monkeypatch.setattr(runner, "load", lambda path: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    yield tmp_path
    session_mod._engine, session_mod._SessionFactory = before


@pytest.fixture
def client(jobs_db, monkeypatch):
    holder = {"cfg": ON}
    webapp = _wire(monkeypatch, holder)
    return webapp.app.test_client(), holder


def post(c, word, job_id):
    return c.post(f"/apply/picks/{word}", json={"job_id": job_id})


def item_of(item_id):
    with get_session() as s:
        return s.get(ApplyItem, item_id)


def picks_now(c):
    d = c.get("/apply/picks").get_json()
    assert d["ok"]
    return d


# -- what the page is sent -----------------------------------------------------------------------------

def test_switched_off_the_page_is_told_so_and_nothing_is_created(client, jobs_db):
    c, holder = client
    holder["cfg"] = OFF
    d = picks_now(c)
    assert (d["enabled"], d["picks"], d["set_aside"], d["report"]) == (False, [], [], {})
    assert (d["per_day"], d["min_score"]) == (5, 80)
    import sqlite3
    with sqlite3.connect(jobs_db / "jobs.db") as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"shortlist_picks", "shortlist_exclusions", "shortlist_links"} & names                # reading created nothing


def test_each_pick_shows_its_job_why_it_was_picked_its_eligibility_and_where_it_stands(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    d = picks_now(c)
    assert [p["job_id"] for p in d["picks"]] == [1, 2, 4, 11, 12] and d["enabled"]
    first = d["picks"][0]
    assert (first["company"], first["title"], first["score"], first["location"]) == ("Globex", "Platform Engineer", 95, "Remote, US")
    assert first["posting"] == "https://greenhouse.example/1"                      # the employer's own hiring system
    assert d["picks"][2]["posting"] == "https://wonka.example/jobs/4411-platform-engineer"   # saved by the person
    assert first["reasons"][0] == "match score 95% (at least 80%)" and shortlist.SPONSORSHIP_UNKNOWN in first["reasons"]
    assert first["entry"]["state"] == "queued" and first["entry"]["label"] == "Queued"
    assert first["entry"]["decision"]["decision"] == "ELIGIBLE_WITH_WARNINGS"
    assert first["can"] == {"remove": True, "defer": True, "prepare": True}
    assert [r["job_id"] for r in d["report"]["left_out"]][:3] == [3, 5, 6]          # and why the others were not


# -- Remove, Not today, Restore ----------------------------------------------------------------------------

def test_remove_takes_a_waiting_pick_off_the_list_for_good_and_restore_brings_it_back(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    item = picks_now(c)["picks"][0]["item_id"]
    r = post(c, "remove", 1)
    assert r.status_code == 200 and r.get_json()["cancelled"] is True
    assert item_of(item).state == "cancelled" and item_of(item).outcome == "Cancelled by you: nothing was sent."
    with get_session() as s:
        assert "Removed from the morning's picks by you" in [e.message for e in s.query(ApplyEvent).filter_by(item_id=item)]
    d = picks_now(c)
    assert [(a["job_id"], a["word"]) for a in d["set_aside"]] == [(1, "removed")]
    tomorrow = shortlist.pick(ON, now=now() + timedelta(days=1))
    why = {r["job_id"]: r["reason"] for r in tomorrow["left_out"]}
    assert why[1] == "you removed it from the morning's picks"
    assert 8 in [p["job_id"] for p in tomorrow["picked"]]                         # Globex's other job took its place
    assert post(c, "restore", 1).status_code == 200 and picks_now(c)["set_aside"] == []
    later = shortlist.pick(ON, now=now() + timedelta(days=2))
    why = {r["job_id"]: r["reason"] for r in later["left_out"]}
    assert why[1] == "another job at Globex is on your chosen list"              # considered again, by every rule
    with get_session() as s:                                                      # that one done with, it can go
        s.query(ApplyItem).filter_by(job_id=8).one().state = "cancelled"
    assert 1 in [p["job_id"] for p in shortlist.pick(ON, now=now() + timedelta(days=3))["picked"]]


def test_not_today_takes_it_off_until_tomorrow_when_it_is_considered_again(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    item = next(p["item_id"] for p in picks_now(c)["picks"] if p["job_id"] == 2)
    r = post(c, "defer", 2).get_json()
    assert r["ok"] and r["word"] == "deferred" and item_of(item).state == "cancelled"
    assert [(a["job_id"], a["word"]) for a in picks_now(c)["set_aside"]] == [(2, "deferred")]
    again_today = shortlist.pick(Cfg({**ON, "shortlist.per_day": 20}), now=now() + timedelta(minutes=1))
    assert 2 not in [p["job_id"] for p in again_today["picked"]]
    assert 2 in [p["job_id"] for p in shortlist.pick(ON, now=now() + timedelta(days=1))["picked"]]


def test_a_job_you_chose_yourself_is_never_touched_from_the_picks(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    execution.add([13])                                                           # yours: not a pick
    with get_session() as s:
        mine = s.query(ApplyItem).filter_by(job_id=13).one().id
    for word in ("remove", "defer"):
        r = post(c, word, 13)
        assert r.status_code == 409 and "not one of the morning's picks" in r.get_json()["error"]
    assert item_of(mine).state == "queued"
    # A pick put off, then added again by you from the Jobs page: Remove leaves your entry alone.
    assert post(c, "defer", 11).status_code == 200
    execution.add([11])
    with get_session() as s:
        yours = s.query(ApplyItem).filter_by(job_id=11, state="queued").one().id
    assert post(c, "remove", 11).get_json() == {"ok": True, "word": "removed", "cancelled": False}
    assert item_of(yours).state == "queued"


@pytest.mark.parametrize("state, words", [("validating", "validating now"), ("waiting_user", "waiting for you now"),
                                          ("pending_confirmation", "pending confirmation now"),
                                          ("applied", "You applied to it already")])
def test_a_pick_on_its_way_waiting_or_applied_to_stays_as_it_is(client, state, words):
    c, _ = client
    shortlist.pick(ON, now=now())
    item = picks_now(c)["picks"][0]["item_id"]
    with get_session() as s:
        s.get(ApplyItem, item).state = state
    for word in ("remove", "defer"):
        r = post(c, word, 1)
        assert r.status_code == 409 and words in r.get_json()["error"]
    assert item_of(item).state == state
    assert picks_now(c)["picks"][0]["can"] == {"remove": False, "defer": False, "prepare": False}
    assert picks_now(c)["set_aside"] == []


def test_presses_without_a_job_or_on_one_never_set_aside_are_refused(client):
    c, _ = client
    for body in ({}, {"job_id": "1"}, {"job_id": True}, {"job_id": -3}):
        assert c.post("/apply/picks/remove", json=body).status_code == 400
    assert post(c, "remove", 1).status_code == 409                               # no picks at all yet
    shortlist.pick(ON, now=now())
    assert post(c, "restore", 2).status_code == 409                              # never removed or put off


def test_none_of_it_prepares_opens_or_sends_anything_or_touches_history(client):
    c, _ = client
    shortlist.pick(ON, now=now())
    post(c, "remove", 1)
    post(c, "defer", 2)
    post(c, "restore", 1)
    with get_session() as s:
        assert s.query(ApplyArtifact).count() == 0
        assert {i.state for i in s.query(ApplyItem)} == {"queued", "cancelled"}
    assert not runner.AUDIT_LOG.exists()


# -- the settings ------------------------------------------------------------------------------------------

def test_the_settings_are_shown_saved_to_their_own_file_and_checked(client):
    c, holder = client
    holder["cfg"] = OFF
    d = c.get("/apply/picks/settings").get_json()
    assert d["settings"]["enabled"] is False and d["settings"]["per_day"] == 5 and d["settings"]["min_fit"] == 0.8
    assert d["workplaces"] == ["remote", "hybrid", "onsite"] and "full-time" in d["job_types"]
    r = c.post("/apply/picks/settings", json={"per_day": 3, "min_fit": 0.85, "required_skills": "Kubernetes, Go",
                                              "workplaces": ["remote"], "max_years_required": 8, "min_salary": None})
    assert r.status_code == 200 and r.get_json()["settings"]["per_day"] == 3
    kept = json.loads(shortlist.SETTINGS.read_text())
    assert kept == {"max_years_required": 8.0, "min_fit": 0.85, "min_salary": None, "per_day": 3,
                    "required_skills": ["Kubernetes", "Go"], "workplaces": ["remote"]}
    assert shortlist.enabled(OFF) is False                                       # saving others switched nothing on
    s = shortlist.settings(OFF)
    assert (s["per_day"], s["min_fit"], s["required_skills"]) == (3, 0.85, ["Kubernetes", "Go"])


@pytest.mark.parametrize("body", [{"per_day": 0}, {"per_day": 2.5}, {"min_fit": 1.5}, {"min_fit": "0.8"},
                                  {"enabled": "yes"}, {"workplaces": ["mars"]}, {"job_types": ["gig"]},
                                  {"max_considered": 9999}, {"color": "red"}, {"min_salary": -1},
                                  {"required_skills": [1, 2]}, [1, 2]])
def test_a_setting_out_of_range_keeps_nothing(client, body):
    c, holder = client
    holder["cfg"] = OFF
    c.post("/apply/picks/settings", json={"per_day": 4})
    before = shortlist.SETTINGS.read_text()
    r = c.post("/apply/picks/settings", json=body)
    assert r.status_code == 400 and r.get_json()["error"]
    assert shortlist.SETTINGS.read_text() == before


def test_switching_on_from_the_settings_is_the_persons_own_press(client):
    c, holder = client
    holder["cfg"] = OFF
    assert c.post("/apply/picks/settings", json={"enabled": True}).get_json()["settings"]["enabled"] is True
    assert shortlist.enabled(OFF) is True                                        # in this test's own file only
    assert c.post("/apply/picks/settings", json={"enabled": False}).get_json()["settings"]["enabled"] is False


# -- the pages render ----------------------------------------------------------------------------------------

def test_both_pages_render_with_their_new_cards_hidden_until_the_app_answers(client):
    c, _ = client
    page = c.get("/apply")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '<section class="card" id="picks-card" hidden' in html and 'id="chosen-card"' in html
    assert html.index('id="picks-card"') < html.index('id="chosen-card"')
    settings = c.get("/setup")
    assert settings.status_code == 200
    assert '<section class="card" id="picks-settings" hidden' in settings.get_data(as_text=True)


# -- in a browser: WebKit and Chromium, a laptop and a phone -----------------------------------------------------

@pytest.fixture
def app_copy(jobs_db, monkeypatch):
    from werkzeug.serving import make_server
    holder = {"cfg": ON}
    webapp = _wire(monkeypatch, holder)
    monkeypatch.setattr(webapp, "task_pid", lambda task: None)
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: None)
    stream = execution.stream
    monkeypatch.setattr(execution, "stream", lambda after, **kw: stream(after, seconds=1, every=0.1))
    server = make_server("127.0.0.1", 0, webapp.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", holder
    server.shutdown()
    time.sleep(1.2)


def _browser(engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        return manager, getattr(manager, engine).launch()
    except Exception as exc:                       # no such browser installed here
        manager.stop()
        pytest.skip(f"no {engine}: {exc}")


def _fits(page):
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("width", [1440, 390])
def test_the_picks_card_in_a_browser(app_copy, engine, width, tmp_path):
    from qa.browser_checks import browser_diagnostics
    base, holder = app_copy
    shortlist.pick(ON, now=now())
    manager, browser = _browser(engine)
    narrow = width < 500
    try:
        page = browser.new_page(viewport={"width": width, "height": 900 if not narrow else 844},
                                service_workers="block", reduced_motion="reduce")
        with browser_diagnostics(page, tmp_path, f"picks-{engine}-{width}"):
            page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(base + "/apply", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelectorAll('#picks-list tr[data-pick]').length === 5")
            assert page.locator("#picks-title").inner_text() == "This morning's picks (5)"
            row = page.locator('#picks-list tr[data-pick="1"]')
            assert "Globex" in row.inner_text() and "Remote, US" in row.inner_text()
            assert row.locator(".pill.pk-st-selected").count() == 1 and row.locator(".dc-eligible_with_warnings").count() == 1
            assert row.locator('a[href="https://greenhouse.example/1"]').count() == 1
            row.locator(".pk-why summary").click()
            assert shortlist.SPONSORSHIP_UNKNOWN in row.locator(".pk-why").inner_text()
            page.wait_for_function("document.querySelectorAll('#chosen-list tr[data-row]').length === 5")
            assert _fits(page), "the page scrolls sideways"

            where = ".ch-acts-narrow" if narrow else ".ch-acts-wide"
            with page.expect_response(lambda r: r.url.endswith("/apply/picks/remove"), timeout=5000):
                row.locator(f'{where} [data-pk="remove"]').click()
            page.wait_for_function("!document.getElementById('picks-aside-box').hidden")
            assert "Removed" in page.locator("#picks-say").inner_text()
            page.wait_for_function("document.querySelectorAll('#chosen-list tr[data-row]').length === 5 && "
                                   "!!document.querySelector('#chosen-list .pill.st-cancelled')")
            page.locator("#picks-aside-box summary").click()
            with page.expect_response(lambda r: r.url.endswith("/apply/picks/restore"), timeout=5000):
                page.locator('#picks-aside [data-pk="restore"][data-job="1"]').click()
            page.wait_for_function("document.getElementById('picks-aside-box').hidden")

            with page.expect_response(lambda r: r.url.endswith("/apply/picks/defer"), timeout=5000):
                page.locator(f'#picks-list tr[data-pick="2"] {where} [data-pk="defer"]').click()
            page.wait_for_function("document.getElementById('picks-say').textContent.startsWith('Put off')")
            page.locator("#picks-left-box summary").click()
            assert "listing site" in page.locator("#picks-left").inner_text()
            assert _fits(page) and not errors
    finally:
        browser.close()
        manager.stop()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
def test_loading_off_blocked_error_and_absent_states_in_a_browser(app_copy, engine, monkeypatch, tmp_path):
    base, holder = app_copy
    manager, browser = _browser(engine)
    try:
        page = browser.new_page(viewport={"width": 390, "height": 844}, service_workers="block")
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        holder["cfg"] = OFF                                                      # switched off: says so
        page.goto(base + "/apply", wait_until="domcontentloaded")
        page.wait_for_function("!document.getElementById('picks-card').hidden")
        assert "Morning picks are off" in page.locator("#picks-note").inner_text()
        assert page.locator("#picks-list tr").count() == 0

        holder["cfg"] = ON                                                       # your details unfinished
        monkeypatch.setattr(runner, "check_ready", lambda applicant: ["your phone number"])
        shortlist.pick(ON, now=now())
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.getElementById('picks-note').textContent.includes('Nothing picked')")
        assert "your phone number" in page.locator("#picks-note").inner_text()

        page.route("**/apply/picks", lambda r: r.fulfill(status=500, body="broken"))   # the app failing
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#picks-retry")
        assert "could not be loaded" in page.locator("#picks-list").inner_text()

        page.unroute("**/apply/picks")                                           # an app without picks yet
        page.route("**/apply/picks", lambda r: r.fulfill(status=404, body="not here"))
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.querySelectorAll('#chosen-list p, #chosen-list table').length > 0")
        assert page.locator("#picks-card").is_hidden()
        assert not errors
    finally:
        browser.close()
        manager.stop()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("width", [1440, 390])
def test_the_settings_card_in_a_browser(app_copy, engine, width):
    base, holder = app_copy
    holder["cfg"] = OFF
    manager, browser = _browser(engine)
    try:
        page = browser.new_page(viewport={"width": width, "height": 900}, service_workers="block")
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/setup#picks-settings", wait_until="domcontentloaded")
        page.wait_for_function("!document.getElementById('picks-settings').hidden && "
                               "document.getElementById('pk-per-day').value === '5'")
        assert page.locator("#pk-min").input_value() == "80" and not page.locator("#pk-enabled").is_checked()
        page.locator("#pk-per-day").fill("3")
        page.locator("#pk-skills").fill("Kubernetes, Terraform")
        page.locator('#pk-workplaces input[value="remote"]').check()
        with page.expect_response(lambda r: r.url.endswith("/apply/picks/settings") and r.request.method == "POST"):
            page.locator("#pk-save").click()
        page.wait_for_function("document.getElementById('pk-out').textContent.startsWith('Saved')")
        assert "Morning picks are off" in page.locator("#pk-out").inner_text()
        kept = json.loads(shortlist.SETTINGS.read_text())
        assert (kept["per_day"], kept["required_skills"], kept["workplaces"], kept["enabled"]) == \
               (3, ["Kubernetes", "Terraform"], ["remote"], False)
        page.locator("#pk-per-day").fill("40")                                  # out of range: the server says so
        page.evaluate("document.getElementById('pk-per-day').removeAttribute('max')")
        page.locator("#pk-save").click()
        page.wait_for_function("document.getElementById('pk-out').className.includes('bad')")
        assert json.loads(shortlist.SETTINGS.read_text())["per_day"] == 3
        assert _fits(page) and not errors
    finally:
        browser.close()
        manager.stop()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("width", [1440, 390])
def test_prepare_from_the_picks_card_in_a_browser(app_copy, engine, width, monkeypatch):
    """Step 3: Prepare on one pick, and on today's picks, start the run for those entries only; a writer
    that bills is used only after the person's yes; each pick says where it stands."""
    from backend.api import app as webapp
    base, holder = app_copy
    started = []
    monkeypatch.setattr("backend.api.shortlist_pages.start_task",
                        lambda task, args: started.append((task, args)) or webapp.jsonify(ok=True))
    monkeypatch.setattr("backend.api.shortlist_pages.task_pid", lambda task: None)
    monkeypatch.setattr(runner, "writer", lambda cfg: ("openai", "OpenAI", False))
    shortlist.pick(ON, now=now())
    manager, browser = _browser(engine)
    narrow = width < 500
    try:
        page = browser.new_page(viewport={"width": width, "height": 900}, service_workers="block")
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        errors, asked = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: (asked.append(d.message), d.accept()))
        page.goto(base + "/apply", wait_until="domcontentloaded")
        page.wait_for_function("document.querySelectorAll('#picks-list tr[data-pick]').length === 5")
        assert page.locator("#picks-prepare").inner_text() == "Prepare today's picks (5)"
        assert page.locator('#picks-list tr[data-pick="1"] .pk-st-selected').inner_text() == "Selected"
        where = ".ch-acts-narrow" if narrow else ".ch-acts-wide"
        with page.expect_response(lambda r: r.url.endswith("/apply/picks/prepare") and r.status == 200, timeout=5000):
            page.locator(f'#picks-list tr[data-pick="2"] {where} [data-pk="prepare"]').click()
        assert asked and "bills per use" in asked[0]                               # asked first, then yes
        (task, args), = started
        with get_session() as s:
            two = s.query(ApplyItem).filter_by(job_id=2).one().id
        assert task == "prepare-chosen" and "--paid-ok" in args and args[-2:] == ["--items", str(two)]
        page.wait_for_function("document.getElementById('picks-say').textContent.startsWith('Making the resume')")
        with page.expect_response(lambda r: r.url.endswith("/apply/picks/prepare") and r.status == 200, timeout=5000):
            page.locator("#picks-prepare").click()
        assert len(started[1][1][started[1][1].index("--items") + 1].split(",")) == 5
        assert _fits(page) and not errors
    finally:
        browser.close()
        manager.stop()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
def test_each_button_on_a_pick_follows_its_own_permission(app_copy, engine):
    """A failed pick may be removed but not put off: Not today is for a pick waiting on the list."""
    base, holder = app_copy
    shortlist.pick(ON, now=now())
    with get_session() as s:
        s.query(ApplyItem).filter_by(job_id=4).one().state = "failed"
    manager, browser = _browser(engine)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        page.goto(base + "/apply", wait_until="domcontentloaded")
        page.wait_for_function("document.querySelectorAll('#picks-list tr[data-pick]').length === 5")
        row = page.locator('#picks-list tr[data-pick="4"] .ch-acts-wide')
        assert row.locator('[data-pk="defer"]').is_disabled() and row.locator('[data-pk="remove"]').is_enabled()
        assert row.locator('[data-pk="prepare"]').count() == 0
        ok = page.locator('#picks-list tr[data-pick="1"] .ch-acts-wide')
        assert ok.locator('[data-pk="defer"]').is_enabled() and ok.locator('[data-pk="prepare"]').is_enabled()
    finally:
        browser.close()
        manager.stop()
