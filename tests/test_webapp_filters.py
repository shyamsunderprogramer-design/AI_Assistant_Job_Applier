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
