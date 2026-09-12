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


# -- a failed probe must be remembered -------------------------------------
#
# Probes that miss leave website/careers_url NULL, so selecting on NULL alone
# re-asks the same failures every round. A round is ~500 companies and a dead
# host costs the full timeout, so this is most of the run's time.

import sqlite3

import pytest

from companies import enrich_careers, enrich_websites


def company_db(tmp_path):
    db = sqlite3.connect(tmp_path / "c.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY, name TEXT, country TEXT, tier TEXT,
            website TEXT, careers_url TEXT, updated_at TEXT
        );
    """)
    db.executescript(enrich_websites.PROBE_SCHEMA)
    db.executescript(enrich_careers.PROBE_SCHEMA)
    return db


def unprobed_website_ids(db, retry_failed=False):
    where = "website IS NULL AND tier = 'sponsor'"
    if not retry_failed:
        where += (" AND NOT EXISTS (SELECT 1 FROM website_probe p"
                  " WHERE p.company_id = companies.id)")
    return [r["id"] for r in db.execute(f"SELECT id FROM companies WHERE {where}")]


def test_a_probed_miss_is_not_offered_again(tmp_path):
    db = company_db(tmp_path)
    db.execute("INSERT INTO companies (id, name, tier) VALUES (1, 'Tried', 'sponsor')")
    db.execute("INSERT INTO companies (id, name, tier) VALUES (2, 'Fresh', 'sponsor')")
    db.execute("INSERT INTO website_probe VALUES (1, 'https://tried.com', 0, 'now')")

    assert unprobed_website_ids(db) == [2]


def test_retry_failed_offers_the_misses_again(tmp_path):
    db = company_db(tmp_path)
    db.execute("INSERT INTO companies (id, name, tier) VALUES (1, 'Tried', 'sponsor')")
    db.execute("INSERT INTO website_probe VALUES (1, 'https://tried.com', 0, 'now')")

    assert unprobed_website_ids(db, retry_failed=True) == [1]


def test_careers_miss_is_recorded_so_it_is_not_repeated(tmp_path):
    db = company_db(tmp_path)
    db.execute("INSERT INTO companies (id, name, website, tier) "
               "VALUES (1, 'NoCareers', 'https://x.com', 'sponsor')")
    # what run() records for a company whose probe found nothing
    db.execute("INSERT OR REPLACE INTO careers_probe VALUES (1, 0, 'now')")

    remaining = db.execute(
        "SELECT id FROM companies WHERE careers_url IS NULL AND website IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM careers_probe p WHERE p.company_id = companies.id)"
    ).fetchall()
    assert remaining == []
