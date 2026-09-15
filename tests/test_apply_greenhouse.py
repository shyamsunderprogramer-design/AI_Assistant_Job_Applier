"""Filling a Greenhouse form — offline, with a fake page. No browser, no network.

Every test here is about a refusal. The filling itself is mechanical; what
matters is the three things this module will not do, because each of them would
send something false out under a real person's name.
"""

from apply.greenhouse import FillResult, _is_declaration, fill
from apply.profile import Applicant


class FakeElement:
    def __init__(self, page, field_id):
        self.page, self.field_id = page, field_id

    def fill(self, value):
        self.page.filled[self.field_id] = value

    def set_input_files(self, path):
        self.page.files[self.field_id] = path

    def click(self):
        self.page.clicked = True


class FakePage:
    """A Greenhouse form, shaped like the real ones actually are."""

    def __init__(self, labels=None, fields=None, captcha=True):
        self.labels = labels or []
        self.fields = set(fields or ["first_name", "last_name", "email", "phone"])
        self.captcha = captcha
        self.filled, self.files = {}, {}
        self.clicked = False

    def goto(self, url, **kw):
        return None

    def wait_for_timeout(self, ms):
        return None

    def query_selector(self, selector):
        name = selector.lstrip("#").split("[")[0]
        if "submit" in selector:
            return FakeElement(self, "submit")
        return FakeElement(self, name) if name in self.fields else None

    def eval_on_selector_all(self, selector, script):
        return self.labels

    def content(self):
        return "<html>g-recaptcha</html>" if self.captcha else "<html>plain</html>"

    def screenshot(self, path=None, full_page=False):
        return None


def person(**over) -> Applicant:
    base = Applicant(
        personal={"first_name": "Dev", "last_name": "P", "email": "d@example.com",
                  "phone": "+1 555 0100"},
        authorisation={"authorised_to_work": True, "requires_sponsorship": False},
        links={"linkedin": "https://linkedin.com/in/dev"},
    )
    for k, v in over.items():
        setattr(base, k, v)
    return base


# -- the ordinary case -----------------------------------------------------

def test_the_standard_fields_are_filled_by_id():
    page = FakePage()
    result = fill(page, 1, "https://example.com/apply", person())

    assert page.filled["first_name"] == "Dev"
    assert page.filled["email"] == "d@example.com"
    assert result.ok


def test_an_employers_own_question_is_matched_by_its_label_not_its_id():
    """Custom question ids are generated per posting — question_7571115009 —
    so the label text is the only stable handle."""
    page = FakePage(labels=[{"text": "LinkedIn Profile*", "id": "question_7571115009"}],
                    fields=["first_name", "question_7571115009"])

    fill(page, 1, "https://example.com/apply", person())

    assert page.filled["question_7571115009"] == "https://linkedin.com/in/dev"


# -- refusal 1: a question we cannot answer is left empty -------------------

def test_an_uncovered_question_is_left_blank_not_approximated():
    page = FakePage(labels=[{"text": "Why do you want to work here?", "id": "question_99"}],
                    fields=["first_name", "question_99"])

    result = fill(page, 1, "https://example.com/apply", person())

    assert "question_99" not in page.filled
    assert any("Why do you want" in blank for blank in result.left_blank)


# -- refusal 2: declarations are never templated ---------------------------

def test_an_unstated_declaration_is_flagged_for_the_person():
    page = FakePage(
        labels=[{"text": "Will you now or in the future require sponsorship?",
                 "id": "question_spon"}],
        fields=["first_name", "question_spon"])
    unknown = person(authorisation={"authorised_to_work": None,
                                    "requires_sponsorship": None})

    result = fill(page, 1, "https://example.com/apply", unknown)

    assert "question_spon" not in page.filled
    assert any("sponsorship" in d.lower() for d in result.declarations)


def test_a_stated_declaration_is_answered():
    page = FakePage(
        labels=[{"text": "Will you now or in the future require sponsorship?",
                 "id": "question_spon"}],
        fields=["first_name", "question_spon"])

    result = fill(page, 1, "https://example.com/apply", person())

    assert page.filled["question_spon"] == "No"
    assert result.declarations == []


def test_declaration_phrasings_are_recognised():
    for label in ("Will you require visa sponsorship?",
                  "Are you legally authorized to work in the United States?",
                  "Do you hold an active security clearance?",
                  "Are you subject to ITAR restrictions?"):
        assert _is_declaration(label), label
    assert not _is_declaration("What is your favourite language?")


# -- refusal 3: a CAPTCHA is a stop, never something to get around ----------

def test_a_captcha_stops_submission_even_when_submit_was_asked_for():
    """Every Greenhouse form sampled carried a reCAPTCHA. Filling is the slow
    part and is still done; the last click belongs to a person."""
    page = FakePage(captcha=True)

    result = fill(page, 1, "https://example.com/apply", person(), submit=True)

    assert result.captcha is True
    assert result.submitted is False
    assert page.clicked is False
    assert result.filled          # the work was still done


def test_without_a_captcha_submit_is_honoured_when_explicitly_asked():
    page = FakePage(captcha=False)

    result = fill(page, 1, "https://example.com/apply", person(), submit=True)

    assert result.submitted is True and page.clicked is True


def test_submit_is_never_the_default():
    page = FakePage(captcha=False)
    assert fill(page, 1, "https://example.com/apply", person()).submitted is False
    assert page.clicked is False


def test_an_outstanding_declaration_blocks_submit_on_its_own():
    """Even with no CAPTCHA: a form with an unanswered legal question must not
    be sent."""
    page = FakePage(labels=[{"text": "Will you require sponsorship?", "id": "q1"}],
                    fields=["first_name", "q1"], captcha=False)
    unknown = person(authorisation={"authorised_to_work": None,
                                    "requires_sponsorship": None})

    result = fill(page, 1, "https://example.com/apply", unknown, submit=True)

    assert result.submitted is False and page.clicked is False


# -- failure is reported, not raised --------------------------------------

def test_an_unreachable_form_is_reported_not_raised():
    class Dead(FakePage):
        def goto(self, url, **kw):
            raise TimeoutError("navigation timeout")

    result = fill(Dead(), 1, "https://example.com/apply", person())

    assert not result.ok and "could not open" in result.error


def test_the_summary_says_what_a_person_still_has_to_do():
    result = FillResult(job_id=1, url="u", filled={"a": "b"}, captcha=True)
    assert "submit is yours" in result.summary()
