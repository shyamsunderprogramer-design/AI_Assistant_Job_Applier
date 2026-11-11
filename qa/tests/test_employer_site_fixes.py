"""Step 4 of the Morning shortlist plan (7 Nov 2026): the three failures of the 27 October run.

  1. Listing-site links (Quora, Cyware via Jobicy): the listing's page is read once and the employer's
     own posting it links is taken -- only one, on a hiring system, of this company's -- or the job is
     left out with the reason. The listing's own link is kept.
  2. The Ashby form that would not open (lumos: net::ERR_ABORTED; frame was detached): the posting is
     opened first, then its form, a page the site moved on from itself is not a failure, a failure is
     tried once more and no more, and a closed posting says so without anything on its page pressed.
  3. A browser check that does not clear (Versa Networks via Himalayas): the job goes to the person's
     own browser -- waiting for their word, never Failed and never Applied.

Made-up jobs in a throwaway database; made-up pages in a browser whose every request to anywhere real
is refused; a reader that never reaches a real listing site.
"""

from datetime import timedelta

import pytest

from backend.apply import anyform, execution, greenhouse, listings, runner, shortlist
from backend.apply.execution import APPLIED, FAILED, PENDING, QUEUED
from data_engineering.companies.careers_detect import detect_url
from data_engineering.companies.careers_finder import Page
from data_engineering.db.models import Job, ShortlistLink
from data_engineering.db.session import get_session
from qa.tests.test_morning_shortlist import NOW, ON, Cfg, add_jobs, db, picked, reasons  # noqa: F401

UUID = "7acb033e-9fce-405b-84af-cfa3acb24870"
OTHER = "0b5c7d1e-1111-2222-3333-444455556666"
LISTING = "https://jobicy.example/30"
QUORA = f"https://jobs.ashbyhq.com/quora/{UUID}"


# -- 1. listing-site links -----------------------------------------------------------------------------

def listing_job(n=30, company="Quora", board="jobicy", **extra):
    add_jobs((n, company, board, 0.99, extra))
    with get_session() as s:
        s.get(Job, n).application_url = f"https://{board}.example/{n}"


class Reader:
    """A listing site, made up: each address's page, and every address asked for."""

    def __init__(self, pages=None):
        self.pages, self.asked = dict(pages or {}), []

    def __call__(self):
        return self.fetch

    def of(self, n):                                  # the made-up data's own listing job (3) aside
        return [u for u in self.asked if u.endswith(f"/{n}")]

    def fetch(self, url, **kw):
        self.asked.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        return page or Page(url=url, final_url=url, status=404)


def html(*links):
    return Page(url=LISTING, final_url=LISTING, status=200, html="<html><body><h1>Platform Engineer</h1>"
                + "".join(f'<a href="{u}">Apply</a>' for u in links) + "</body></html>")


def row(job_id):
    with get_session() as s:
        job = s.get(Job, job_id)
        links = s.query(ShortlistLink).filter_by(job_id=job_id).order_by(ShortlistLink.id).all()
        return job.application_url, job.employer_url, job.employer_url_kind, \
            [(r.from_url, r.url, r.outcome) for r in links]


def test_a_listing_is_followed_to_the_employers_own_posting_and_its_own_link_is_kept(db):
    listing_job()
    reader = Reader({LISTING: html(f"{QUORA}/application?utm_source=jobicy", "https://jobicy.example/jobs")})
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert 30 in picked(report) and reader.of(30) == [LISTING]
    why = next(p["reasons"] for p in report["picked"] if p["job_id"] == 30)
    assert f"the employer's own posting (jobs.ashbyhq.com), found from its listing on jobicy.example: {QUORA}" \
           " (its listing's own link is kept)" in why
    assert row(30) == (LISTING, QUORA, "posting", [(LISTING, QUORA, "found")])   # never guessed: the one it links


def test_a_listing_that_redirects_into_the_employers_hiring_system_is_followed(db):
    listing_job()
    posting = "https://job-boards.greenhouse.io/quora/jobs/4462594009"
    reader = Reader({LISTING: Page(url=LISTING, final_url=posting, stopped_at_ats=detect_url(posting))})
    assert 30 in picked(shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader))
    assert row(30)[1] == posting


@pytest.mark.parametrize("links, why", [
    ((f"https://jobs.ashbyhq.com/acme/{UUID}",), "links no posting of Quora's"),           # another company's advert
    (("https://jobs.ashbyhq.com/quora",), "links no posting of Quora's"),                  # its list of jobs, not one
    ((QUORA, f"https://jobs.ashbyhq.com/quora/{OTHER}"), "links 2 different postings of Quora's, so none is taken"),
    ((), "links no posting of Quora's"),                                                   # an apply form of its own
])
def test_nothing_is_guessed_an_unconfirmed_listing_is_left_out_with_the_reason(db, links, why):
    listing_job()
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=Reader({LISTING: html(*links)}))
    assert 30 not in picked(report)
    assert why in reasons(report)[30] and reasons(report)[30].startswith(
        "its link goes to a listing site (jobicy) and no employer posting was confirmed")
    assert row(30)[:3] == (LISTING, None, None) and row(30)[3][0][2] == "none"


def test_a_site_that_turns_the_reader_away_is_not_asked_again_for_a_week(db):
    listing_job()
    reader = Reader({LISTING: Page(url=LISTING, final_url=LISTING, error="robots")})
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert "its rules (robots.txt) ask apps not to read it" in reasons(report)[30]
    shortlist.pick(Cfg(ON), now=NOW + timedelta(days=1), fetch_for=reader)
    assert reader.of(30) == [LISTING]                                      # remembered, not read again
    shortlist.pick(Cfg(ON), now=NOW + timedelta(days=8), fetch_for=reader)
    assert reader.of(30) == [LISTING, LISTING]


def test_a_listing_that_could_not_be_read_is_tried_again_the_next_day_and_found(db):
    listing_job()
    reader = Reader({LISTING: Page(url=LISTING, final_url=LISTING, error="timeout")})
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert "could not be read (timeout); it is tried again tomorrow" in reasons(report)[30]
    shortlist.pick(Cfg(ON), now=NOW + timedelta(hours=2), fetch_for=reader)        # the top-up: not yet
    assert reader.of(30) == [LISTING]
    reader.pages[LISTING] = html(QUORA)
    report = shortlist.pick(Cfg(ON), now=NOW + timedelta(days=1), fetch_for=reader)
    assert 30 in picked(report)
    assert [o for _, _, o in row(30)[3]] == ["error", "found"]                     # every look kept


def test_a_reader_that_breaks_costs_that_job_only(db):
    listing_job()
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=Reader({LISTING: RuntimeError("socket closed")}))
    assert "could not be read (RuntimeError)" in reasons(report)[30]
    assert len(report["picked"]) == 5                                              # the morning goes on


def test_an_email_alerts_link_is_never_followed(db):
    listing_job(board="ziprecruiter-email")
    reader = Reader()
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert reader.of(30) == [] and "save the employer's posting on the job's page" in reasons(report)[30]


def test_a_careers_site_the_person_saved_is_left_as_theirs(db):
    listing_job(employer_url="https://quora.example/careers", employer_url_kind="careers")
    reader = Reader({LISTING: html(QUORA)})
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert reader.of(30) == [] and 30 not in picked(report)
    assert row(30)[1] == "https://quora.example/careers"


def test_a_posting_found_then_taken_off_the_jobs_page_is_not_found_again(db):
    listing_job()
    reader = Reader({LISTING: html(QUORA)})
    shortlist.ensure_tables()
    listings.keep(30, LISTING, listings.Look(listings.FOUND, QUORA, "found"), NOW)
    with get_session() as s:
        s.get(Job, 30).employer_url = None                                         # the person took it off
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert reader.of(30) == [] and "was taken off the job's page" in reasons(report)[30]


def test_a_morning_makes_a_few_looks_at_most(db, monkeypatch):
    monkeypatch.setattr(listings, "MAX_LOOKS", 2)
    for n in (30, 31, 32):
        listing_job(n, company=f"Quora{n}")
    reader = Reader()
    report = shortlist.pick(Cfg(ON), now=NOW, fetch_for=reader)
    assert len(reader.asked) == 2
    assert "this morning's looks are used up" in reasons(report)[32]


# -- 2. Ashby: the posting first, then its form -------------------------------------------------------------

POSTING_PAGE = (f'<html><head><title>Platform Engineer @ Lumos</title></head><body><h1>Platform Engineer</h1>'
                f'<a href="/lumos/{UUID}/application">Apply for this Job</a></body></html>')
FORM_PAGE = ('<html><head><title>Apply · Lumos</title></head><body><form>'
             '<label for="n">Name*</label><input id="n" name="name"><label for="e">Email*</label>'
             '<input id="e" type="email" name="email"><label for="p">Phone</label><input id="p" type="tel">'
             '<label for="r">Resume*</label><input id="r" type="file"><button type="submit">Submit Application</button>'
             '</form></body></html>')
LIST_PAGE = (f'<html><head><title>Jobs at Lumos</title></head><body><h1>Open roles</h1>'
             f'<a href="/lumos/{OTHER}">Another job</a> <a href="/lumos/{OTHER}/application">Apply now</a></body></html>')
LUMOS = f"https://jobs.ashbyhq.com/lumos/{UUID}"


@pytest.fixture
def browser():
    """A real browser that can reach nothing: every request not answered by a test's own page is refused."""
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        chromium = manager.chromium.launch()
    except Exception as exc:                       # no browser installed here
        manager.stop()
        pytest.skip(f"no Chromium: {exc}")
    context = chromium.new_context()
    asked = []

    def nowhere(route):
        asked.append(route.request.url)
        route.abort()
    context.route("**/*", nowhere)
    page = context.new_page()
    yield page, asked
    chromium.close()
    manager.stop()


def serve(page, pages: dict, asked: list):
    """Each address's answer: a page, a redirect ("->url"), or "abort" for a load the browser gives up on."""
    def answer(route):
        url = route.request.url
        asked.append(url)
        todo = pages.get(url)
        if callable(todo):
            todo = todo()
        if todo == "abort":
            route.abort("aborted")
        elif isinstance(todo, str) and todo.startswith("->"):
            route.fulfill(status=302, headers={"Location": todo[2:]})
        elif todo:
            route.fulfill(status=200, content_type="text/html", body=todo)
        else:
            route.abort()
    page.route("https://jobs.ashbyhq.com/**", answer)


def test_an_ashby_form_link_opens_its_posting_first_then_its_form_trying_a_failed_load_once_more(browser):
    page, asked = browser
    tries = iter(["abort", FORM_PAGE])                       # the form gives up once, as on 27 October
    serve(page, {LUMOS: POSTING_PAGE, f"{LUMOS}/application": lambda: next(tries)}, asked)
    reached = anyform.reach(page, f"{LUMOS}/application", say=lambda s: None, check_s=5)
    assert reached.error is None and reached.kind == "form" and reached.url == f"{LUMOS}/application"
    assert asked == [LUMOS, f"{LUMOS}/application", f"{LUMOS}/application"]


def test_a_form_that_never_opens_is_tried_twice_and_no_more_and_says_why(browser):
    page, asked = browser
    serve(page, {LUMOS: POSTING_PAGE, f"{LUMOS}/application": "abort"}, asked)
    reached = anyform.reach(page, f"{LUMOS}/application", say=lambda s: None, check_s=5)
    assert reached.kind is None and reached.stop is None
    assert reached.error.startswith("could not open its form (") and reached.error.endswith("tried 2 times)")
    assert asked.count(f"{LUMOS}/application") == 2 and len(asked) == 3


@pytest.mark.parametrize("answer", ["->https://jobs.ashbyhq.com/lumos", "list-in-place"])
def test_a_closed_ashby_posting_says_so_and_no_other_jobs_apply_is_pressed(browser, answer):
    page, asked = browser
    pages = {"https://jobs.ashbyhq.com/lumos": LIST_PAGE}
    pages[LUMOS] = answer if answer.startswith("->") else LIST_PAGE.replace("Open roles", "Job not found")
    serve(page, pages, asked)
    reached = anyform.reach(page, f"{LUMOS}/application", say=lambda s: None, check_s=5)
    assert reached.stop == "gone" and reached.error.startswith("the posting has closed")
    assert not any(OTHER in url for url in asked)


class FakePage:
    """A browser page, made up: what each load does to it."""

    def __init__(self, loads, url="about:blank"):
        self.loads, self.url, self.closed, self.goes = list(loads), url, False, 0

    def goto(self, url, **kw):
        self.goes += 1
        what = self.loads.pop(0)
        if isinstance(what, str) and what.startswith("http"):
            self.url = what                                # the site moved on to here while loading
            raise RuntimeError("Page.goto: net::ERR_ABORTED; maybe frame was detached?")
        if isinstance(what, Exception):
            raise what
        self.url = url

    def is_closed(self):
        return self.closed

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass


def test_a_page_the_site_moved_on_from_while_loading_is_opened_not_failed():
    page = FakePage([f"{LUMOS}?redirected=1"])
    assert anyform.open_page(page, f"{LUMOS}/application") is None and page.goes == 1


def test_a_load_that_times_out_is_tried_once_more_then_given_up():
    page = FakePage([TimeoutError("Timeout 45000ms exceeded."), TimeoutError("Timeout 45000ms exceeded.")])
    assert anyform.open_page(page, LUMOS) == "could not open the posting (Timeout 45000ms exceeded.; tried 2 times)"
    assert page.goes == 2
    page = FakePage([TimeoutError("slow"), None])
    assert anyform.open_page(page, LUMOS) is None and page.goes == 2


def test_an_abort_that_left_the_page_where_it_was_is_not_taken_for_arriving():
    page = FakePage([RuntimeError("net::ERR_ABORTED"), RuntimeError("net::ERR_ABORTED")], url="https://jobicy.example/30")
    assert anyform.open_page(page, LUMOS).startswith("could not open the posting (net::ERR_ABORTED")


# -- 3. browser checks ---------------------------------------------------------------------------------------

CHECK_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
CLEARS = ("<html><head><title>Just a moment...</title></head><body><div id=x>Checking</div><script>"
          "setTimeout(() => { document.title = 'Apply'; document.body.innerHTML = "
          + repr(FORM_PAGE.split("<body>")[1].split("</body>")[0]).replace("\\'", "'") + "; }, 800)</script></body></html>")


def test_a_check_that_never_clears_stops_with_a_check_not_a_failure(browser):
    page, asked = browser
    page.route("https://careers.example/**", lambda route: route.fulfill(status=200, content_type="text/html",
                                                                         body=CHECK_PAGE))
    said = []
    reached = anyform.reach(page, "https://careers.example/jobs/9", say=said.append, check_s=3)
    assert reached.stop == "check" and reached.error == "the site's browser check did not clear"
    assert any("checking the browser" in s for s in said) and asked == []      # nothing else was asked


def test_a_check_that_clears_goes_on_to_the_form(browser):
    page, _ = browser
    page.route("https://careers.example/**", lambda route: route.fulfill(status=200, content_type="text/html",
                                                                         body=CLEARS))
    reached = anyform.reach(page, "https://careers.example/jobs/9", say=lambda s: None, check_s=20)
    assert reached.error is None and reached.kind == "form"


class CheckingPage(FakePage):
    """A check page that reloads itself as it works: one wait is cut short, then it clears."""

    def __init__(self, clears_after, cut=1):
        super().__init__([])
        self.waits, self.clears_after, self.cut = 0, clears_after, cut

    def title(self):
        return "Apply" if self.waits >= self.clears_after else "Just a moment..."

    def wait_for_timeout(self, ms):
        self.waits += 1
        if self.cut:
            self.cut -= 1
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")


def test_a_check_page_reloading_itself_is_waited_out_not_given_up_on():
    page = CheckingPage(clears_after=3)
    assert anyform.wait_out_check(page, say=lambda s: None, timeout_s=60, sleep=lambda s: None) is True


def test_a_closed_window_ends_the_wait_and_is_not_called_a_check():
    page = CheckingPage(clears_after=99, cut=0)
    page.closed = True
    assert anyform.wait_out_check(page, say=lambda s: None, timeout_s=60, sleep=lambda s: None) is False
