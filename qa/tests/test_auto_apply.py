"""Auto-apply: the job-site account, Workday sign-in, the morning's questions and answers."""

import pytest

from backend.apply import accounts, prepare


class MemoryKeyring:
    """A stand-in for the macOS Keychain, so tests never touch the real one."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture
def keychain(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.setattr(accounts, "_keyring", lambda: kr)
    return kr


def test_the_password_lives_only_in_the_keychain(keychain):
    accounts.save("me@example.com", "correct-horse-9")
    assert accounts.credentials() == ("me@example.com", "correct-horse-9")
    assert accounts.status() == {"email": "me@example.com", "saved": True}
    accounts.forget()
    assert accounts.status()["saved"] is False


@pytest.mark.parametrize("email,password", [("not-an-email", "longenough1"), ("me@example.com", "short")])
def test_a_bad_account_is_refused(keychain, email, password):
    with pytest.raises(ValueError):
        accounts.save(email, password)


class FakeEl:
    def __init__(self, page, aid):
        self.page, self.aid = page, aid

    def is_visible(self):
        return True

    def fill(self, value):
        self.page.filled[self.aid] = value

    def click(self):
        self.page.clicked.append(self.aid)


class FakePage:
    def __init__(self, fields):
        self.fields, self.filled, self.clicked = set(fields), {}, []

    def query_selector(self, selector):
        for aid in self.fields:
            if f'"{aid}"' in selector:
                return FakeEl(self, aid)
        return None

    def wait_for_timeout(self, ms):
        pass


def test_workday_signs_in_with_the_saved_account(keychain):
    accounts.save("me@example.com", "correct-horse-9")
    page = FakePage({"email", "password", "signInSubmitButton"})
    assert accounts.workday_sign_in(page, {}, say=lambda s: None) == "signing-in"
    assert page.filled == {"email": "me@example.com", "password": "correct-horse-9"}
    assert page.clicked == ["signInSubmitButton"]


def test_a_new_account_is_filled_but_the_person_agrees_and_creates_it(keychain):
    accounts.save("me@example.com", "correct-horse-9")
    page = FakePage({"email", "password", "verifyPassword", "createAccountSubmitButton"})
    assert accounts.workday_sign_in(page, {}, say=lambda s: None) == "create-filled"
    assert page.filled["verifyPassword"] == "correct-horse-9"
    assert page.clicked == []                     # never presses Create Account itself


def test_without_a_saved_account_the_person_signs_in(keychain):
    assert accounts.workday_sign_in(FakePage({"password"}), {}, say=lambda s: None) == "no-account-saved"


class Job:
    source, company_slug, external_id, company = "greenhouse", "acme", "123", "Acme"


class Applicant:
    def answer_for(self, label):
        return "Yes" if "authorized" in label.lower() else None


def test_only_questions_without_an_answer_are_listed(monkeypatch):
    class Reply:
        def json(self):
            return {"questions": [
                {"label": "First Name", "required": True, "fields": [{"name": "first_name"}]},
                {"label": "Are you authorized to work in the US?", "required": True, "fields": [{"name": "question_1"}]},
                {"label": "Why Acme?", "required": True, "fields": [{"name": "question_2", "type": "textarea"}]},
                {"label": "Pronouns", "required": False,
                 "fields": [{"name": "question_3", "values": [{"label": "she/her"}, {"label": "he/him"}]}]},
            ]}
    monkeypatch.setattr(prepare.requests, "get", lambda *a, **k: Reply())
    asked = prepare.open_questions(Job(), Applicant())
    assert [q["label"] for q in asked] == ["Why Acme?", "Pronouns"]
    assert asked[1]["options"] == ["she/her", "he/him"]


def test_answers_are_kept_for_the_job_and_its_questions_close(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "PREPARED", tmp_path / "prepared.json")
    monkeypatch.setattr("backend.apply.answers.BANK_PATH", tmp_path / "answers.yaml")
    prepare._save({"7": {"job_id": 7, "company": "Acme", "state": "ready",
                         "questions": [{"label": "Why Acme?"}, {"label": "Notice period"}]}})
    prepare.save_answers(7, {"Why Acme?": "Your platform team ships weekly.", "Notice period": ""})
    entry = prepare.load()["7"]
    assert entry["answers"] == {"Why Acme?": "Your platform team ships weekly."}
    assert [q["label"] for q in entry["questions"]] == ["Notice period"]
    prepare.mark(7, "applied")
    assert prepare.pending() == []


def test_the_morning_picks_from_the_queue_by_job(monkeypatch, tmp_path):
    """The queue hands back job ids; each one is prepared from its job."""
    from data_engineering.db.session import init_engine
    from data_engineering.db.models import Job as JobRow
    from data_engineering.db.session import get_session
    init_engine("sqlite:///:memory:")
    with get_session() as s:
        s.add(JobRow(source="greenhouse", company="Acme", company_slug="acme", external_id="1",
                     title="SRE", location="Remote", description="x " * 400, requirements="",
                     application_url="https://example.com/1", content_hash="h1", is_open=True,
                     ats_match_score=0.9, score_basis="full"))
    monkeypatch.setattr(prepare, "PREPARED", tmp_path / "prepared.json")
    monkeypatch.setattr("backend.apply.profile.load", lambda path: Applicant())
    seen = []
    monkeypatch.setattr(prepare, "prepare_one", lambda cfg, job, applicant, say: seen.append(job.title) or
                        {"job_id": job.id, "state": "ready"})

    class Cfg:
        def get(self, key, default=None):
            return default
    prepare.prepare(Cfg(), say=lambda s: None)
    assert seen == ["SRE"] and prepare.pending()[0]["job_id"]
