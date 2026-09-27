"""Guided key sign-up — offline. Nothing here opens a browser or the network."""

import pytest

from backend.core import key_signup
from backend.core.key_signup import (PROVIDERS, candidates, field_value, find_working_key,
                                     guide, prefill)

PERSON = {"email": "me@example.com", "first_name": "Sam", "last_name": "D",
          "full_name": "Sam D", "linkedin": "https://linkedin.com/in/sam"}
by = {p.name: p for p in PROVIDERS}


# -- what gets filled -----------------------------------------------------------

@pytest.mark.parametrize("descriptor,expected", [
    ("email Email address", "me@example.com"),
    ("first_name First Name", "Sam"),
    ("lastname Last name", "D"),
    ("organization Company / Organization", "Independent (personal job search)"),
    ("website Your website URL", "https://linkedin.com/in/sam"),
    ("purpose Describe how you will use the API", key_signup.PURPOSE),
    ("name Your name", "Sam D"),
])
def test_non_secret_fields_are_filled(descriptor, expected):
    assert field_value(descriptor, PERSON) == expected


@pytest.mark.parametrize("descriptor", ["password Password", "confirm_password Confirm password",
                                        "captcha", "phone Phone number", "username User name",
                                        "app_name Application name"])
def test_secrets_and_unknowns_are_never_filled(descriptor):
    assert field_value(descriptor, PERSON) is None


class FakePage:
    def __init__(self, fields):
        self.fields, self.filled, self.closed = fields, {}, False
        self.url = "https://developer.usajobs.gov/APIRequest/Index"

    def evaluate(self, js):
        return self.fields

    def fill(self, selector, value):
        self.filled[selector] = value

    def is_closed(self):
        return self.closed

    def goto(self, *a, **k): pass
    def wait_for_timeout(self, ms): pass
    def inner_text(self, sel): return ""
    def close(self): self.closed = True


def test_prefill_skips_passwords_checkboxes_and_filled_fields():
    page = FakePage([
        {"idx": 0, "type": "email", "value": "", "visible": True, "text": "Email"},
        {"idx": 1, "type": "password", "value": "", "visible": True, "text": "Password"},
        {"idx": 2, "type": "checkbox", "value": "", "visible": True, "text": "I agree to the terms"},
        {"idx": 3, "type": "text", "value": "typed already", "visible": True, "text": "First name"},
        {"idx": 4, "type": "text", "value": "", "visible": False, "text": "Last name"},
    ])
    prefill(page, PERSON)
    assert page.filled == {"[data-signup-idx='0']": "me@example.com"}


# -- finding and testing the key -----------------------------------------------

def test_adzuna_pairs_every_id_with_every_key():
    text = "Application ID 1a2b3c4d  Application Keys 0123456789abcdef0123456789abcdef"
    assert candidates(text, by["adzuna"]) == [
        {"ADZUNA_APP_ID": "1a2b3c4d", "ADZUNA_APP_KEY": "0123456789abcdef0123456789abcdef"}]


def test_no_candidates_when_part_of_the_pair_is_missing():
    assert candidates("Application ID 1a2b3c4d", by["adzuna"]) == []


def test_only_a_key_that_works_is_accepted():
    text = "keys: 11111111-2222-3333-4444-555555555555 and aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    good = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    found = find_working_key(text, by["jooble"], set(),
                             check=lambda v: v["JOOBLE_API_KEY"] == good)
    assert found == {"JOOBLE_API_KEY": good}


def test_candidates_are_tested_a_bounded_number_of_times():
    calls = []
    text = " ".join(f"{i:08x}-0000-0000-0000-000000000000" for i in range(20))
    tried = set()
    find_working_key(text, by["jooble"], tried, check=lambda v: calls.append(v) or False)
    find_working_key(text, by["jooble"], tried, check=lambda v: calls.append(v) or False)
    assert len(calls) <= key_signup.MAX_TRIES_PER_PROVIDER


# -- the guided loop -------------------------------------------------------------

class FakeContext:
    def __init__(self, page_text=""):
        self.page_text, self.pages = page_text, []

    def new_page(self):
        page = FakePage([])
        page.inner_text = lambda sel: self.page_text
        self.pages.append(page)
        return page


def test_a_working_key_on_the_page_is_saved(monkeypatch):
    saved = {}
    monkeypatch.setattr(key_signup, "save", lambda values: saved.update(values) or list(values))
    monkeypatch.setattr(by["findwork"], "check", lambda v: True)
    ctx = FakeContext("Your API token: " + "ab" * 20)
    result = guide(by["findwork"], ctx, PERSON, wait_minutes=1, poll_s=0, mail_every_s=999,
                   sleep=lambda s: None)
    assert result == "saved" and saved == {"FINDWORK_API_KEY": "ab" * 20}


def test_closing_the_tab_skips_the_provider(monkeypatch):
    ctx = FakeContext("")
    real_new = ctx.new_page

    def closed_page():
        page = real_new()
        page.closed = True
        return page

    ctx.new_page = closed_page
    assert guide(by["findwork"], ctx, PERSON, wait_minutes=1, poll_s=0, mail_every_s=999,
                 sleep=lambda s: None) == "skipped"


def test_nothing_found_times_out_without_saving(monkeypatch):
    monkeypatch.setattr(key_signup, "save", lambda values: pytest.fail("saved"))
    ticks = iter(range(0, 10_000, 30))
    assert guide(by["findwork"], FakeContext("no key here"), PERSON, wait_minutes=1, poll_s=0,
                 mail_every_s=999, now=lambda: next(ticks), sleep=lambda s: None) == "timed out"


def test_providers_with_keys_are_skipped(monkeypatch):
    for p in PROVIDERS:
        for k in p.env:
            monkeypatch.setenv(k, "set")
    assert key_signup.run(context=FakeContext()) == {}


def test_a_form_that_appears_late_is_still_filled_once(monkeypatch):
    """Jooble's form shows only after Cloudflare's check has passed."""
    monkeypatch.setattr(key_signup, "save", lambda values: list(values))
    monkeypatch.setattr(by["jooble"], "check", lambda v: True)
    ctx = FakeContext("")
    page = ctx.new_page()
    ctx.new_page = lambda: page
    rounds = {"n": 0}

    def fields(js):
        rounds["n"] += 1
        if rounds["n"] < 3:
            return []                         # still verifying
        # The form appears; once submitted, the key shows on the page.
        ctx.page_text = "Your key: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        return [{"idx": 0, "type": "email", "value": "", "visible": True, "text": "Email"}]

    page.evaluate = fields
    ticks = iter(range(0, 10_000))
    assert guide(by["jooble"], ctx, PERSON, wait_minutes=1, poll_s=0, mail_every_s=999,
                 now=lambda: next(ticks), sleep=lambda s: None) == "saved"
    assert rounds["n"] == 3                   # filled once, then left alone
    assert page.filled == {"[data-signup-idx='0']": "me@example.com"}
