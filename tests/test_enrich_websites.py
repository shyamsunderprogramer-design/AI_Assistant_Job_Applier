"""Website-guessing tests — offline, no requests leave the machine.

These runs probe 500 companies at a time and take hours. The failure that
matters is not a missed website; it is one malformed name aborting the batch
and throwing away every probe after it, which is what happened to a run on
2026-09-11.
"""

import requests

from companies.enrich_websites import MAX_LABEL, slug_candidates, verify


# -- slugs must be things DNS can actually carry ---------------------------

def test_ordinary_name_becomes_a_slug():
    assert slug_candidates("Acme Widgets Ltd") == ["acmewidgets"]


def test_long_name_also_offers_its_first_word():
    out = slug_candidates("Northern Digital Engineering Partners")
    assert "northerndigitalengineeringpartners" in out
    assert "northern" in out


def test_oversized_label_is_dropped_not_emitted():
    # A DNS label is capped at 63 octets; longer ones raise inside the idna
    # codec, past the exception requests promises to raise.
    name = " ".join(["verylongword"] * 10)
    assert all(len(s) <= MAX_LABEL for s in slug_candidates(name))


def test_name_with_nothing_usable_yields_nothing():
    assert slug_candidates("...") == []
    assert slug_candidates("") == []


# -- one bad host must not end the run ------------------------------------

def test_unicode_error_is_caught(monkeypatch):
    def explode(*a, **k):
        raise UnicodeError("label empty or too long")
    monkeypatch.setattr(requests, "get", explode)

    assert verify("https://whatever.nl", None) is False


def test_request_failure_is_caught(monkeypatch):
    def explode(*a, **k):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "get", explode)

    assert verify("https://whatever.nl", None) is False


def test_value_error_is_caught(monkeypatch):
    def explode(*a, **k):
        raise ValueError("invalid URL")
    monkeypatch.setattr(requests, "get", explode)

    assert verify("https://.nl", None) is False
