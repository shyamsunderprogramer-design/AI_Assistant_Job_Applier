"""Learning the person's own answers, and submitting past an invisible CAPTCHA.

Both exist so an application can go out the way a person would send it: with
the answers they gave last time to the same question, and a Submit press that
an invisible score-only CAPTCHA judges like any other.
"""

import pytest

from backend.apply import answers, greenhouse, runner
from backend.apply.answers import learnable, normalise, remember
from backend.apply.greenhouse import captcha_is_invisible, fill
from backend.apply.profile import Applicant

from qa.tests.test_apply_runner import Context, Page, add_job, db, person, ready  # noqa: F401

GREENHOUSE_HTML = ('<script src="https://www.recaptcha.net/recaptcha/enterprise.js'
                   '?render=6LfmcbcpAAAAAChNTbhUShzUOAMj"></script>'
                   '<div class="grecaptcha-badge"></div>')


# -- which CAPTCHA asks something of a person -----------------------------

def test_greenhouse_score_only_recaptcha_is_invisible():
    assert captcha_is_invisible(GREENHOUSE_HTML)


def test_a_checkbox_or_hcaptcha_is_not():
    assert not captcha_is_invisible('<div class="g-recaptcha"></div>'
                                    '<script src="https://www.google.com/recaptcha/api.js"></script>')
    assert not captcha_is_invisible("recaptcha/api.js?render=explicit")
    assert not captcha_is_invisible(GREENHOUSE_HTML + '<iframe src="hcaptcha.com"></iframe>')


class InvisiblePage(Page):
    def content(self):
        return GREENHOUSE_HTML


def test_an_invisible_captcha_does_not_stop_the_submit(tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    page = InvisiblePage()
    result = fill(page, 1, "u", person(), resume, submit=True)
    assert not result.captcha and result.submitted and page.submitted


def test_it_still_never_submits_with_a_question_unanswered(tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_text("x")
    page = InvisiblePage(fields=["q9"], labels=[("Desired Salary*", "q9")])
    result = fill(page, 1, "u", person(), resume, submit=True)
    assert not page.submitted and "Desired Salary*" in result.required_blank


# -- what may be learned --------------------------------------------------

def test_wording_is_normalised_but_not_paraphrased():
    assert normalise("Desired Salary *") == normalise("desired salary?")
    assert normalise("Desired salary") != normalise("Salary expectations")


def test_short_answers_are_learned():
    assert learnable("Are you at least 18 years of age?*", "Yes")
    assert learnable("Desired Salary*", "$170,000")


def test_a_paragraph_is_never_learned():
    assert not learnable("Why do you want this role?", "Because " * 30)


def test_nothing_naming_the_employer_is_learned():
    assert not learnable("What makes you uniquely qualified for this role at CMT?",
                         "Ten years", company="Cambridge Mobile Telematics (CMT)")
    assert not learnable("How did you hear about us?", "A Five9 recruiter", company="five9")


def test_identity_and_files_are_never_learned():
    assert not learnable("First Name*", "Jordan")
    assert not learnable("Resume/CV", "resume.pdf")


def test_remember_writes_and_the_profile_reads_it_back():
    learned = remember({"Desired Salary*": "$170,000", "First Name": "Jordan"})
    assert learned == ["desired salary"]
    a = Applicant(answers=answers.load())
    assert a.answer_for("Desired salary ") == "$170,000"


def test_the_profile_wins_over_a_learned_answer():
    """A learned answer fills gaps; it never overrides a stated profile field."""
    remember({"Will you now or in the future require sponsorship?": "Yes"})
    a = person()
    a.answers = answers.load()
    assert a.answer_for("Will you now or in the future require sponsorship?") == "No"


# -- the run learns from the person's own submission ---------------------

def test_answers_given_by_hand_are_used_on_the_next_form(db, ready, monkeypatch):
    add_job()
    monkeypatch.setattr(greenhouse, "watch_submission",
                        lambda page, timeout_s, **kw: ("submitted", {"Desired Salary*": "$170,000"}))
    outcomes = runner.run(ready, limit=5, browser_factory=lambda: Context(captcha=True))
    assert outcomes[0].result == "submitted"
    assert answers.load() == {"desired salary": "$170,000"}


def test_a_skipped_form_teaches_nothing(db, ready, monkeypatch):
    add_job()
    monkeypatch.setattr(greenhouse, "watch_submission",
                        lambda page, timeout_s, **kw: ("skipped", {"Desired Salary*": "$1"}))
    runner.run(ready, limit=5, browser_factory=lambda: Context(captcha=True))
    assert answers.load() == {}


def test_address_fields_are_left_to_the_profile():
    """Greenhouse's "Country" is the phone code picker; it reads back "+1"."""
    assert not learnable("Country*", "+1")
    assert not learnable("Location (City)*", "Austin, Texas, United States")
