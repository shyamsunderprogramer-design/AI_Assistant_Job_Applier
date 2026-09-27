"""The apply run end to end, and the form behaviour it depends on — no browser.

The fakes are shaped like the live Greenhouse embed form read in Sep 2026:
Country, Location and every Yes/No are `role=combobox` dropdowns whose options
appear as `[role=option]` once opened, and a required field carries both
`aria-required` and a `*` on its label.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import greenhouse, runner
from backend.apply.greenhouse import await_submission, fill, form_url, is_confirmed
from backend.apply.profile import Applicant
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine


# -- fakes ----------------------------------------------------------------

class Option:
    def __init__(self, dropdown, text):
        self.dropdown, self.text = dropdown, text

    def inner_text(self):
        return self.text

    def click(self):
        self.dropdown.page.filled[self.dropdown.field_id] = self.text


class Element:
    def __init__(self, page, field_id, *, options=None, required=False):
        self.page, self.field_id = page, field_id
        self.options = options              # None: a text box
        self.required = required

    def get_attribute(self, name):
        if name == "role":
            return "combobox" if self.options is not None else None
        if name == "aria-required":
            return "true" if self.required else None
        return None

    def click(self):
        if self.field_id == "submit":
            self.page.submitted = True
            self.page.confirmed = self.page.confirm_on_submit
        elif self.options is not None:
            self.page.open = self

    def fill(self, value):
        if self.options is None:
            self.page.filled[self.field_id] = value
        else:
            self.page.typed[self.field_id] = value      # filters; chooses nothing

    def press(self, key):
        self.page.open = None

    def set_input_files(self, path):
        self.page.files[self.field_id] = path


class Page:
    def __init__(self, *, fields=(), dropdowns=None, labels=(), required=(),
                 captcha=False, confirm_on_submit=True):
        self.elements = {}
        for field_id in ["first_name", "last_name", "email", "phone", *fields]:
            self.elements[field_id] = Element(self, field_id, required=field_id in required)
        for field_id, options in (dropdowns or {}).items():
            self.elements[field_id] = Element(self, field_id, options=options,
                                              required=field_id in required)
        self.elements["resume"] = Element(self, "resume")
        self.labels = [{"text": text, "id": fid} for text, fid in labels]
        self.captcha, self.confirm_on_submit = captcha, confirm_on_submit
        self.filled, self.typed, self.files = {}, {}, {}
        self.open = None
        self.submitted = self.confirmed = self.closed = False
        self.url = ""

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        if self.closed:
            raise RuntimeError("Target closed")

    def query_selector(self, selector):
        if "submit" in selector:
            return Element(self, "submit")
        field_id = selector.split("#")[-1].split("[")[0]
        return self.elements.get(field_id)

    def query_selector_all(self, selector):
        if self.open is None:
            return []
        return [Option(self.open, text) for text in self.open.options]

    def eval_on_selector_all(self, selector, script):
        return self.labels

    def content(self):
        return "<html>g-recaptcha</html>" if self.captcha else "<html></html>"

    def inner_text(self, selector):
        return "Thank you for applying!" if self.confirmed else "Apply for this job"

    def screenshot(self, **kw):
        return None

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


def person(**over) -> Applicant:
    a = Applicant(
        personal={"first_name": "Dev", "last_name": "P", "email": "d@example.com",
                  "phone": "+1 555 0100"},
        location={"city": "Austin", "country": "United States"},
        authorisation={"authorised_to_work": True, "requires_sponsorship": False},
        employment={"current_employer": "Acme", "current_title": "SRE"},
        demographics={"gender": "decline"},
        apply={"stop_before_submit": False},
    )
    for key, value in over.items():
        setattr(a, key, value)
    return a


# -- the form URL ---------------------------------------------------------

def test_every_greenhouse_posting_opens_the_bare_form():
    """A careers-page link frames the form where the filler cannot reach it."""
    assert form_url("five9", "6113941004") == (
        "https://job-boards.greenhouse.io/embed/job_app?for=five9&token=6113941004")


# -- dropdowns ------------------------------------------------------------

def test_a_dropdown_is_answered_by_picking_the_matching_option():
    page = Page(dropdowns={"country": ["United States Minor Outlying Islands",
                                       "United States"]})
    fill(page, 1, "u", person())
    assert page.filled["country"] == "United States"      # exact beats prefix


def test_a_dropdown_takes_an_option_that_starts_with_the_answer():
    page = Page(dropdowns={"candidate-location": ["Austin, Texas, United States"]})
    fill(page, 1, "u", person())
    assert page.filled["candidate-location"] == "Austin, Texas, United States"


def test_a_yes_no_question_is_chosen_not_typed():
    page = Page(dropdowns={"q_auth": ["Yes", "No"]},
                labels=[("Are you authorized to work in the US? *", "q_auth")])
    fill(page, 1, "u", person())
    assert page.filled["q_auth"] == "Yes"


def test_no_matching_option_leaves_the_dropdown_empty():
    page = Page(dropdowns={"country": ["Canada", "Mexico"]}, required={"country"})
    result = fill(page, 1, "u", person())
    assert "country" not in page.filled
    assert "country" in result.required_blank


def test_decline_picks_whatever_the_form_calls_declining():
    page = Page(dropdowns={"gender": ["Male", "Female", "Decline To Self Identify"]})
    fill(page, 1, "u", person())
    assert page.filled["gender"] == "Decline To Self Identify"


def test_current_company_and_title_come_from_the_profile():
    page = Page(fields=["q1", "q2"],
                labels=[("Current Company*", "q1"), ("Current Title*", "q2")])
    fill(page, 1, "u", person())
    assert page.filled["q1"] == "Acme" and page.filled["q2"] == "SRE"


# -- what stops an automatic submit --------------------------------------

def test_a_blank_required_question_stops_an_automatic_submit():
    page = Page(fields=["q9"], labels=[("Why do you want to work here?*", "q9")])
    result = fill(page, 1, "u", person(), submit=True)
    assert result.required_blank and not page.submitted


def test_a_resume_that_did_not_attach_stops_an_automatic_submit(tmp_path):
    page = Page()
    del page.elements["resume"]
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    result = fill(page, 1, "u", person(), resume, submit=True)
    assert "resume" in result.required_blank and not page.submitted


def test_everything_answered_and_no_captcha_submits(tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    page = Page()
    result = fill(page, 1, "u", person(), resume, submit=True)
    assert result.submitted and page.submitted


# -- waiting for the person ----------------------------------------------

def test_a_confirmation_page_counts_as_submitted():
    page = Page()
    page.confirmed = True
    assert is_confirmed(page)
    assert await_submission(page, 5) == "submitted"


def test_closing_the_tab_skips_the_job():
    page = Page()
    page.closed = True
    assert await_submission(page, 5) == "skipped"


def test_no_answer_in_time_is_a_timeout_not_an_application():
    page = Page()
    ticks = iter(range(0, 1000, 10))
    assert await_submission(page, 30, clock=lambda: next(ticks)) == "timeout"


# -- daily caps ----------------------------------------------------------

class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_the_lower_of_the_two_caps_wins():
    assert runner.remaining_today(Cfg({"limits.max_applications_per_day": 15}),
                                  person(apply={"max_per_day": 10}), 3) == 7
    assert runner.remaining_today(Cfg({"limits.max_applications_per_day": 4}),
                                  person(apply={"max_per_day": 10}), 3) == 1
    assert runner.remaining_today(Cfg({"limits.max_applications_per_day": 4}),
                                  person(), 9) == 0


# -- the whole run -------------------------------------------------------

@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


def add_job(**over) -> int:
    fields = dict(source="greenhouse", company="Acme", company_slug="acme",
                  external_id="123", title="SRE", application_url="https://x/apply",
                  content_hash="h", status="Not Applied", ats_match_score=0.9,
                  posted_at=datetime.now(timezone.utc) - timedelta(hours=2))
    fields.update(over)
    with get_session() as s:
        job = Job(**fields)
        s.add(job)
        s.flush()
        return job.id


class Context:
    def __init__(self, **page_kw):
        self.pages, self.page_kw = [], page_kw

    def new_page(self):
        page = Page(**self.page_kw)
        self.pages.append(page)
        return page


@pytest.fixture
def ready(monkeypatch, tmp_path):
    resume = tmp_path / "base_resume.pdf"
    resume.write_text("my resume")
    monkeypatch.setattr(runner, "load", lambda path: person())
    monkeypatch.setattr(runner, "choose_resume", lambda cfg, job, tailor=True: (resume, "base resume"))
    monkeypatch.setattr(runner, "cover_letter", lambda cfg, job, evidence: None)
    return Cfg({"limits.max_applications_per_day": 15, "resume.min_score": 0.5})


def test_a_submitted_application_is_recorded(db, ready, capsys):
    job_id = add_job()
    ctx = Context()

    outcomes = runner.run(ready, limit=5, browser_factory=lambda: ctx)

    assert [o.result for o in outcomes] == ["submitted"]
    assert ctx.pages[0].url == form_url("acme", "123")
    with get_session() as s:
        job = s.get(Job, job_id)
        assert job.status == "Applied" and job.applied_at is not None
    line = json.loads(runner.AUDIT_LOG.read_text().splitlines()[0])
    assert line["job_id"] == job_id and line["how"] == "auto"


def test_a_captcha_waits_and_a_closed_tab_changes_nothing(db, ready, monkeypatch):
    job_id = add_job()
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("skipped", {}))

    outcomes = runner.run(ready, limit=5, browser_factory=lambda: Context(captcha=True))

    assert [o.result for o in outcomes] == ["skipped"]
    with get_session() as s:
        assert s.get(Job, job_id).status == "Not Applied"
    assert not runner.AUDIT_LOG.exists()


def test_the_person_submitting_after_a_captcha_counts(db, ready, monkeypatch):
    add_job()
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))

    outcomes = runner.run(ready, limit=5, browser_factory=lambda: Context(captcha=True))

    assert outcomes[0].result == "submitted" and outcomes[0].how == "you"


def test_a_job_the_person_already_marked_is_never_applied_to(db, ready):
    add_job(status="Rejected")
    assert runner.run(ready, limit=5, browser_factory=Context) == []


def test_a_missing_profile_stops_before_any_browser_opens(db, monkeypatch):
    from backend.apply.profile import ProfileIncomplete

    def missing(path):
        raise ProfileIncomplete("No applicant profile")

    monkeypatch.setattr(runner, "load", missing)
    with pytest.raises(runner.NotReady):
        runner.run(Cfg(), browser_factory=lambda: pytest.fail("browser opened"))


def test_todays_cap_is_respected(db, ready):
    for n in range(3):
        add_job(company=f"Done{n}", external_id=f"d{n}", status="Applied",
                applied_at=datetime.now(timezone.utc))
    add_job()
    ready["limits.max_applications_per_day"] = 3
    assert runner.run(ready, limit=5, browser_factory=Context) == []


def test_right_to_work_is_a_declaration_left_for_the_person():
    """Five9 asks "Select which best describes your right to work in the US?"
    — a legal statement with several options, never one to pick for someone."""
    page = Page(dropdowns={"q_rtw": ["US Citizen", "Green Card", "Visa"]},
                labels=[("Select which best describes your right to work in the US?*", "q_rtw")])
    result = fill(page, 1, "u", person())
    assert "q_rtw" not in page.filled
    assert any("right to work" in d for d in result.declarations)


def test_address_questions_are_answered_from_the_profile():
    """Two Six asked "Please provide your City *" — worded, not a known id."""
    a = person()
    a.location.update({"state": "TX", "postal_code": "77701"})
    assert a.answer_for("Please provide your City *") == "Austin"
    assert a.answer_for("Please select your State*") == "TX"
    assert a.answer_for("Please provide your Zip Code *") == "77701"
    assert a.answer_for("Are you a US citizen? (citizenship)") is None
    assert a.answer_for("Personal statement") is None
