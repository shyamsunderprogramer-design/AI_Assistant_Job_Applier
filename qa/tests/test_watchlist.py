"""The watch list: companies followed by name however boards write them, and jobs saved for later."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.core import watchlist
from data_engineering.db import session as session_mod
from data_engineering.db.models import Company, Job
from data_engineering.db.session import get_session, init_engine


@pytest.fixture
def db(monkeypatch, tmp_path):
    before = (session_mod._engine, session_mod._SessionFactory)    # put back afterwards, for the tests after
    init_engine("sqlite:///:memory:")
    monkeypatch.setattr(watchlist, "PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist, "BOARD_FILE", tmp_path / "board.csv")
    monkeypatch.setattr(watchlist, "BOARDS_CSV", tmp_path / "boards.csv")
    monkeypatch.setattr(watchlist, "find_board", lambda cfg, key: None)        # no network in tests
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as s:
        s.add(Company(source="greenhouse", slug="acme", name="ACME, Inc.", board_url="https://boards/acme"))
        s.add(Company(source="lever", slug="hooliai", name="Hooli AI", board_url="https://jobs.lever.co/hooliai"))
        for i, (company, title, open_, score, found) in enumerate([
                ("ACME, Inc.", "Platform Engineer", True, 0.81, now - timedelta(days=3)),
                ("acme", "Site Reliability Engineer", True, 0.66, now - timedelta(days=1)),
                ("Acme Rockets", "DevOps Engineer", True, 0.9, now),                 # another company
                ("ACME, Inc.", "Old Role", False, 0.5, now - timedelta(days=40)),     # closed: not listed
                ("Hooli AI", "Staff Cloud Engineer", True, 0.86, now)]):
            s.add(Job(id=i + 1, source="greenhouse", company=company, company_slug=company.lower().replace(" ", ""),
                      external_id=str(i), content_hash=f"h{i}", title=title, status="Not Applied", is_open=open_,
                      ats_match_score=score, found_at=found, application_url=f"https://a/{i}",
                      closed_at=None if open_ else now - timedelta(days=2),
                      description="Apply by March 3, 2027 to be considered." if i == 0 else "Run CI/CD."))
    yield tmp_path
    session_mod._engine, session_mod._SessionFactory = before


def test_one_key_per_company_however_it_is_written():
    assert watchlist.company_key("ACCENTURE LLP") == watchlist.company_key("Accenture") == "accenture"
    assert watchlist.company_key("Hooli AI") == watchlist.company_key("HooliAI")
    assert watchlist.company_key("ABBOTT POINT OF CARE INC. (AN ABBOTT COMPANY)") == "abbottpointofcare"
    assert watchlist.company_key("Accenture Federal Services") != watchlist.company_key("Accenture")


def test_a_watched_company_shows_its_open_roles_best_fit_first_and_what_is_new(db):
    watchlist.add_company("Acme")
    [acme] = watchlist.companies_view()
    assert [r["title"] for r in acme["jobs"]] == ["Platform Engineer", "Site Reliability Engineer"]
    assert acme["boards"] == [{"system": "greenhouse", "slug": "acme", "url": "https://boards/acme"}]
    assert acme["new"] == 0                                  # what was there when it was added is not "new"
    with get_session() as s:
        s.add(Job(id=99, source="greenhouse", company="Acme", company_slug="acme", external_id="99", content_hash="h99",
                  title="Cloud Engineer", status="Not Applied", is_open=True, ats_match_score=0.7,
                  found_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5),
                  application_url="https://a/99"))
    [acme] = watchlist.companies_view()
    assert acme["new"] == 1 and [r["title"] for r in acme["jobs"] if r["new"]] == ["Cloud Engineer"]
    watchlist.mark_seen(acme["key"])
    with get_session() as s:                                  # arrived before the look: no longer new
        s.get(Job, 99).found_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assert watchlist.companies_view()[0]["new"] == 0


def test_auto_apply_knows_the_jobs_at_a_watched_company(db):
    """Apply to next 10 takes a watched company's roles right after the saved jobs (23 Oct 2026),
    matched the way the Watch list page matches them: "ACME, Inc." and "acme" are Acme."""
    with get_session() as s:
        jobs = s.query(Job).all()
        assert watchlist.watched_job_ids(jobs) == set()               # nothing watched yet
        watchlist.add_company("Acme")
        assert watchlist.watched_job_ids(jobs) == {1, 2, 4}           # not Acme Rockets, not Hooli AI


def test_the_apply_next_button_starts_one_run_and_clears_a_job_pages_mark(db, monkeypatch):
    """Apply to next 10 on the Auto-apply page: refused while an apply run's browser is open; else
    started, with the job-page mark cleared so that job's page does not take this run as its own."""
    from backend.api import app as webapp
    from backend.api import auto_apply
    marker = db / "webapp_apply.job"
    marker.write_text("7")
    monkeypatch.setattr(auto_apply, "MARKER", marker)
    started = []
    monkeypatch.setattr(webapp, "start_task", lambda task, args: started.append((task, args)) or {"ok": True})
    client = webapp.app.test_client()

    monkeypatch.setattr(webapp, "task_pid", lambda task: 123 if task == "apply-ready" else None)
    r = client.post("/apply/next/start")
    assert r.status_code == 409 and "already" in r.get_json()["error"] and started == [] and marker.exists()

    monkeypatch.setattr(webapp, "task_pid", lambda task: None)
    assert client.post("/apply/next/start").get_json() == {"ok": True}
    assert started == [("apply", ["apply", "--limit", "10"])] and not marker.exists()


def test_twice_is_refused_by_the_companys_own_name_and_removing_works(db):
    watchlist.add_company("Acme")
    with pytest.raises(watchlist.NotWatched, match="Acme is on your watch list already"):
        watchlist.add_company("ACME, Inc.")
    with pytest.raises(watchlist.NotWatched):
        watchlist.add_company("   ")
    assert watchlist.remove_company("acme") and watchlist.companies_view() == []


def test_a_board_link_names_the_company_and_is_read_now(db):
    read = []
    entry = watchlist.add_company("https://jobs.lever.co/hooliai", on_board=read.append)
    assert entry["name"] == "Hooli AI" and entry["lookup"]["system"] == "lever"
    import time
    for _ in range(50):
        if read:
            break
        time.sleep(0.02)
    assert read and read[0]["key"] == "hooliai"
    with pytest.raises(watchlist.NotWatched):
        watchlist.add_company("Hooli AI")


def test_a_found_board_joins_the_daily_fetch_once(db):
    assert watchlist.add_board("ashby", "initech", "Initech", "https://jobs.ashbyhq.com/initech")
    assert not watchlist.add_board("ashby", "initech", "Initech")                    # already on it
    with get_session() as s:
        row = s.query(Company).filter(Company.source == "ashby", Company.slug == "initech").one()
        assert row.active and row.origin == "watch-list"
    assert "ashby,initech,Initech" in (db / "boards.csv").read_text()


def test_saved_jobs_say_where_they_stand(db):
    watchlist.star(1); watchlist.star(4); watchlist.star(5); watchlist.star(5, on=False)
    jobs = {j["id"]: j for j in watchlist.jobs_view()}
    assert set(jobs) == {1, 4}
    assert jobs[1]["open"] and jobs[1]["deadline"] == "March 3, 2027"
    assert not jobs[4]["open"] and jobs[4]["closed"]
    assert watchlist.starred_ids() == {1, 4}


def test_the_closing_date_is_read_only_when_the_posting_says_it():
    assert watchlist.deadline("Applications close on 11/15/2026.") == "11/15/2026"
    assert watchlist.deadline("Deadline to apply: Dec 1, 2026") == "Dec 1, 2026"
    assert watchlist.deadline("We closed 3 rounds of funding in 2024.") is None


def test_the_routes_add_list_and_star(db, monkeypatch):
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: None)
    monkeypatch.setattr("backend.api.watch_pages.cfg", lambda: None)
    client = webapp.app.test_client()
    assert client.get("/watchlist").status_code == 200
    r = client.post("/watchlist/companies", json={"name": "Acme"})
    assert r.get_json()["ok"] and r.get_json()["entry"]["name"] == "Acme"
    assert client.post("/watchlist/companies", json={"name": "acme"}).status_code == 400
    assert client.post("/job/2/watch", json={"on": True}).get_json() == {"ok": True, "on": True}
    data = client.get("/watchlist/data").get_json()
    assert [c["name"] for c in data["companies"]] == ["Acme"] and [j["id"] for j in data["jobs"]] == [2]
    assert client.get("/watchlist/names?q=hoo").get_json()["names"] == ["Hooli AI"]
