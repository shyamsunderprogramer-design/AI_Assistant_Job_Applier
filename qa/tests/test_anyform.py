"""Any posting, any form: followed from a job board to the employer's form, filled, never submitted."""

import pytest

from backend.apply import anyform, general
from backend.apply.profile import Applicant

BOARD = """<html><head><title>Sr. Cloud Engineer at Example Co | Jobs board</title></head><body>
<p>Sign up for alerts <input type="email" placeholder="Your email"></p>
<a href="/companies/example">Example Co</a>
<a href="https://careers.example.com/jobs/7-sr-cloud-engineer?utm_source=board">Apply now</a></body></html>"""

# Built like a Teamtailor page: the form shows when "Apply Here" is pressed, its questions in
# fieldset legends, and hidden helper text that is no one's label.
CAREERS = """<html><head><title>Sr. Cloud Engineer - Example Co</title></head><body>
<h1>Sr. Cloud Engineer</h1>
<button id="go" onclick="document.getElementById('f').hidden = false">Apply Here</button>
<form id="f" hidden action="/thanks" method="get">
 <fieldset><legend><span>US Work Eligibility<sup aria-hidden="true">*</sup><span class="sr-only">Required</span></span>
   <div>Are you currently eligible to work in the United States for any employer?</div></legend>
   <input type="radio" id="e1" name="a[0]" value="true"><label for="e1">Yes</label>
   <input type="radio" id="e2" name="a[0]" value="false"><label for="e2">No</label></fieldset>
 <fieldset><legend>Sponsorship* Are you authorized to work in the U.S. for any employer without a sponsorship for
   employment visa status (e.g., H-1B)?</legend>
   <input type="radio" id="s1" name="a[1]" value="true"><label for="s1">Yes</label>
   <input type="radio" id="s2" name="a[1]" value="false"><label for="s2">No</label></fieldset>
 <fieldset><legend>Contracting Have you ever worked directly for a U.S. Federal Government agency?</legend>
   <input type="radio" id="c1" name="a[2]" value="true"><label for="c1">Yes</label>
   <input type="radio" id="c2" name="a[2]" value="false"><label for="c2">No</label></fieldset>
 <label for="pn">Preferred name</label><input id="pn" name="a[3][text]">
 <label for="loc">Working Location*<span class="sr-only">Required</span></label><input id="loc" name="a[4][text]">
 <label for="fn">First name*</label><input id="fn" name="candidate[first_name]">
 <label for="ln">Last name*</label><input id="ln" name="candidate[last_name]">
 <label>Email* <input type="email" name="candidate[email]"></label>
 <label for="ph">Phone*</label><input type="tel" id="ph" name="candidate[phone]">
 <label for="st">State</label><select id="st"><option value="">Choose…</option><option value="TX">Texas</option>
   <option value="CO">Colorado</option></select>
 <label for="cv">Upload resume*</label><input type="file" id="cv">
 <label for="more">Additional files</label><input type="file" id="more">
 <label for="cl">Cover letter</label><textarea id="cl"></textarea>
 <div style="display:none">Please wait for the video answers to finish processing before submitting.</div>
 <div><input type="checkbox" id="ok" name="candidate[consent_given]">
   <label for="ok">Required. By submitting this application, I authorize Example Co to keep my data.</label></div>
 <input type="submit" name="commit" value="Send application">
</form></body></html>"""

THANKS = "<html><body><h1>Thank you for applying!</h1><p>We have received your application.</p></body></html>"


def person(**auth):
    return Applicant(personal={"first_name": "Jordan", "last_name": "Example", "email": "jordan@example.com",
                               "phone": "+1 555 010 0199"},
                     location={"city": "Denver", "state": "CO", "postal_code": "80202", "country": "United States"},
                     authorisation={"authorised_to_work": True, "requires_sponsorship": False, **auth})


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        browser = manager.chromium.launch()
    except Exception as exc:                       # no browser installed here
        manager.stop()
        pytest.skip(f"no Chromium: {exc}")
    page = browser.new_page()

    def serve(route):
        url = route.request.url
        body = BOARD if "board.example" in url else THANKS if "/thanks" in url else CAREERS
        route.fulfill(status=200, content_type="text/html", body=body)

    page.route("https://**/*", serve)
    yield page
    browser.close()
    manager.stop()


def test_a_board_posting_is_followed_to_the_employers_form(page):
    said = []
    reached = anyform.reach(page, "https://board.example/jobs/sr-cloud-engineer", say=said.append)
    assert reached.error is None and reached.kind == "form"
    assert reached.url.startswith("https://careers.example.com/jobs/7")   # the board's link, then "Apply Here"
    assert any("Apply now" in s for s in said) and any("Apply Here" in s for s in said)


def test_the_form_is_filled_from_the_profile_and_left_for_the_person(page, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    reached = anyform.reach(page, "https://careers.example.com/jobs/7-sr-cloud-engineer", say=lambda s: None)
    result = anyform.fill(page, 1, reached.url, person(), resume, None, letter_text="Dear Example Co,\n\nHello.",
                          evidence=tmp_path)
    got = {k.split(" Are you")[0].split(" Have you")[0]: v for k, v in result.filled.items()}
    assert got["First name"] == "Jordan" and got["Last name"] == "Example" and got["Phone"] == "+1 555 010 0199"
    assert got["Email"] == "jordan@example.com" and got["Preferred name"] == "Jordan"
    assert got["Working Location"] == "Denver, CO" and got["State"] == "Colorado"
    assert got["Cover letter"].startswith("Dear Example Co")
    assert got["US Work Eligibility"] == "Yes"
    assert got["Sponsorship"] == "Yes"                 # needing none IS a yes, asked "without sponsorship"
    assert "Contracting" not in got                    # the person's own experience: theirs to answer
    assert result.attachments == ["resume=resume.pdf"]  # the resume once, not into "Additional files"
    assert page.evaluate("() => document.getElementById('cv').files.length") == 1
    # The consent box is the person's, named by its own words, never the hidden helper text.
    assert result.declarations and result.declarations[0].startswith("By submitting this application")
    assert not page.evaluate("() => document.getElementById('ok').checked")
    assert not any("video answers" in x for x in result.left_blank + result.declarations)
    assert (tmp_path / "form.png").exists()


def test_submit_is_outlined_and_a_thank_you_page_counts(page, tmp_path):
    anyform.reach(page, "https://careers.example.com/jobs/7", say=lambda s: None)
    result = anyform.fill(page, 1, page.url, person(), None, None, screenshot=False)
    assert anyform.show_submit(page, scroll=False)
    assert "245, 158, 11" in page.evaluate("() => document.querySelector('[name=commit]').style.outline")
    page.evaluate("() => setTimeout(() => document.getElementById('f').submit(), 300)")   # the person presses it
    verdict, _, last = anyform.watch(page, person(), result, 15, evidence=tmp_path, say=lambda s: None, poll_ms=300)
    assert verdict == "submitted" and "/thanks" in last.url
    assert "Thank you for applying" in (tmp_path / "confirmation.txt").read_text()


def test_closing_the_tab_skips_it(page):
    anyform.reach(page, "https://careers.example.com/jobs/7", say=lambda s: None)
    result = anyform.fill(page, 1, page.url, person(), None, None, screenshot=False)
    page.close()
    verdict, _, _ = anyform.watch(page, person(), result, 5, say=lambda s: None, poll_ms=200)
    assert verdict == "skipped"


def test_a_later_page_of_the_form_is_filled_when_it_shows(page):
    page.set_content("<form><label for='a'>First name</label><input id='a'>"
                     "<label for='e'>Email</label><input id='e' type='email'><input id='z'><input type='file'>"
                     "<button type='button' onclick=\"document.getElementById('two').hidden=false\">Next</button>"
                     "<div id='two' hidden><label for='c'>City</label><input id='c'></div></form>")
    seen: set[str] = set()
    first = anyform.fill(page, 1, "about:blank", person(), None, None, seen=seen, screenshot=False)
    assert "City" not in first.filled
    page.click("text=Next")
    again = anyform.fill(page, 1, "about:blank", person(), None, None, seen=seen, screenshot=False)
    assert list(again.filled) == ["City"] and again.filled["City"] == "Denver"


def test_known_boards_and_their_forms():
    assert anyform.board_of("https://job-boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert anyform.board_of("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x") == "workday"
    assert anyform.board_of("https://acme.applytojob.com/apply/AbC123/Engineer") == "jazzhr"
    assert anyform.board_of("https://careers.bixal.com/jobs/1") is None
    assert anyform.greenhouse_form("https://boards.greenhouse.io/acme/jobs/123?gh_src=x").endswith("for=acme&token=123")
    assert anyform.greenhouse_form("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=9") \
        .endswith("for=acme&token=9")
    uid = "0f6e2b9c-1d2e-4f5a-8b9c-0d1e2f3a4b5c"
    assert anyform.apply_path(f"https://jobs.lever.co/acme/{uid}?lever-source=x") == f"https://jobs.lever.co/acme/{uid}/apply"
    assert anyform.apply_path(f"https://jobs.ashbyhq.com/acme/{uid}") == f"https://jobs.ashbyhq.com/acme/{uid}/application"
    assert anyform.apply_path("https://careers.bixal.com/jobs/1") is None


def test_options_are_chosen_by_what_they_say():
    assert anyform.choose(["Yes", "No"], "No") == 1
    assert anyform.choose(["Texas", "Colorado"], "CO") == 1            # a state's code, a list of names
    assert anyform.choose(["Male", "Female", "I don't wish to answer"], "decline") == 2
    assert anyform.choose(["0-2 years", "3-5 years", "6+ years"], "9") == 2
    assert anyform.choose(["Yes", "No"], None) is None


def test_without_sponsorship_is_asked_the_other_way_round():
    asks = "Are you authorized to work in the U.S. for any employer without a sponsorship for employment visa status?"
    assert person().answer_for(asks) == "Yes"
    assert person(requires_sponsorship=True).answer_for(asks) == "No"
    assert person().answer_for("Will you now or in the future require sponsorship?") == "No"
    assert person().answer_for("Are you currently eligible to work in the United States for any employer?") == "Yes"


def test_a_yes_no_question_that_says_state_is_not_an_address():
    assert person().answer_for("Have you supported a Federal or State agency before?") is None
    assert person().answer_for("State") == "CO"
    assert person().answer_for("What state do you live in?") == "CO"


def test_a_federal_agency_is_not_this_employer():
    assert general.kind_of("Have you ever worked directly for a U.S. Federal Government agency?") is None
    assert general.kind_of("Prior Employment with Bixal Have you ever been previously employed by Bixal?").id \
        == "worked_here"


def test_did_it_go_is_kept_until_the_person_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(anyform, "UNCONFIRMED", tmp_path / "unconfirmed.json")
    assert anyform.unconfirmed(5) is None
    anyform.keep_unconfirmed(5, {"company": "Example Co", "filled": {"Email": "x"}})
    assert anyform.unconfirmed(5)["company"] == "Example Co"
    anyform.forget_unconfirmed(5)
    assert anyform.unconfirmed(5) is None
