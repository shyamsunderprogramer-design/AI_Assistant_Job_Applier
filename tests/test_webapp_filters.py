"""The board filter must offer every source the database actually holds.

Workable was added as a fifth ATS and the dropdown was not updated, so 133
boards and their postings were unreachable through the only control the page
offers for narrowing by source -- silently, with no error and no empty state
to hint at it.
"""

import re

import webapp.app as webapp
from scraper.runner import SCRAPER_TYPES


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
    assert page.count('role="radiogroup"') == 2
    for qid in ("q-authorised", "q-sponsorship"):
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

    from jobage import age_days, humanize

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
    assert "let sortKey = 'ageDays', sortDir = 1;" in page
    assert 'data-k="ageDays" aria-sort="ascending"' in page
