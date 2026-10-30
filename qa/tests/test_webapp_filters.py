"""The board filter must offer every source the database actually holds.

Workable was added as a fifth ATS and the dropdown was not updated, so 133
boards and their postings were unreachable through the only control the page
offers for narrowing by source -- silently, with no error and no empty state
to hint at it.
"""

import re

import pytest

import backend.api.app as webapp
from data_engineering.scraper.runner import SCRAPER_TYPES


@pytest.fixture(autouse=True)
def own_settings_and_jobs(monkeypatch):
    """The project's default settings, never the person's .env, and a database of the test's own with
    one made-up job: these read the Jobs page, never the person's jobs (25 Oct 2026; until then they
    opened data/jobs.db whenever no test had set a database up first)."""
    from datetime import datetime, timezone

    import yaml

    from backend.config.loader import DEFAULT_CONFIG_PATH, Config
    from data_engineering.db import session
    from data_engineering.db.models import Job
    before = (session._engine, session._SessionFactory)
    session.init_engine("sqlite:///:memory:")
    with session.get_session() as s:
        s.add(Job(id=1, source="greenhouse", company="Globex", company_slug="globex", external_id="1",
                  content_hash="h1", title="Platform Engineer", description="We build.",
                  application_url="https://example.com/1", posted_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(webapp, "_cfg", Config(yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}))
    yield
    session._engine, session._SessionFactory = before


def test_every_scraper_can_be_filtered_for():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    offered = set(re.findall(r'<option value="([a-z-]+)"', page))
    missing = set(SCRAPER_TYPES) - offered
    assert not missing, f"the board filter cannot reach: {sorted(missing)}"


def test_the_declarations_name_their_question():
    """Both radio groups announced as a bare "Yes / No" to assistive tech,
    on two legal declarations where answering the wrong one matters."""
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        page = client.get("/applicant").get_data(as_text=True)
    assert page.count('role="radiogroup"') == 4
    for qid in ("q-authorised", "q-sponsorship", "q-citizen", "q-greencard"):
        assert f'aria-labelledby="{qid}"' in page
        assert f'id="{qid}"' in page


# --- age: exact, and newest first -----------------------------------------

def test_the_page_is_sent_an_exact_age_not_a_label_to_re_parse():
    """The page used to derive the sortable age by parsing "3 days" back out
    of the label. That lost the precision, and it could not read "just now"
    at all -- so a posting thirty seconds old sorted as though its age were
    unknown, which is the opposite of what it is."""
    import re

    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert '"ageDays"' in page, "the row data carries no exact age"
    # And the page no longer re-derives it from the label.
    assert "const ageDays = label" not in page


def test_just_now_has_a_real_age():
    from datetime import datetime, timedelta, timezone

    from backend.core.jobage import age_days, humanize

    class Fresh:
        posted_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        found_at = None

    assert humanize(timedelta(seconds=30)) == "just now"
    days = age_days(Fresh())
    assert days is not None and 0 < days < 0.001


def test_the_table_sorts_newest_first_by_default():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert "let sorts = [{k: 'ageDays', dir: 1}" in page
    assert 'data-k="ageDays" aria-sort="ascending"' in page
