"""Worldwide companies reference database.

A SEPARATE dataset from data/jobs.db: that one tracks live job postings for the
application pipeline (see db/models.py); this one is a vetted company directory —
who the company is, where it is, and whether it is known to sponsor
international candidates.

Layers:
  1. Tier lists       — Fortune Global 500, Forbes Global 2000 (stable, verified)
  2. Sponsor registry — official government lists (UK Home Office, US USCIS/DOL,
                        Netherlands IND). A licence is proof the company is real,
                        operating, and has sponsored visas — exactly the
                        "no fake + sponsorship info" filter.
  3. Enrichment       — website / careers URL / contacts, verified over time.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "companies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    website TEXT,
    careers_url TEXT,
    country TEXT,          -- ISO 3166-1 alpha-2 where known
    region TEXT,           -- state / province / county
    city TEXT,
    industry TEXT,
    size_estimate TEXT,
    contact_email TEXT,
    contact_url TEXT,
    tier TEXT NOT NULL DEFAULT 'other',  -- fortune500 | forbes2000 | sponsor | other
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_name_country
    ON companies(name_normalized, ifnull(country, ''));
CREATE INDEX IF NOT EXISTS idx_companies_country ON companies(country);

CREATE TABLE IF NOT EXISTS sponsorship (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    country TEXT NOT NULL,        -- country doing the sponsoring (ISO alpha-2)
    program TEXT NOT NULL,        -- H-1B | Skilled Worker | IND Recognised | ...
    status TEXT NOT NULL,         -- active | provisional | withdrawn (per register)
    source TEXT NOT NULL,         -- registry the fact came from
    source_detail TEXT,           -- route, rating, approval counts — registry-specific
    first_seen TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    UNIQUE(company_id, country, program, source)
);
CREATE INDEX IF NOT EXISTS idx_sponsorship_company ON sponsorship(company_id);
CREATE INDEX IF NOT EXISTS idx_sponsorship_country ON sponsorship(country);

CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_matched INTEGER NOT NULL,
    notes TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(name: str) -> str:
    """Case/space/punctuation-insensitive key for dedupe across sources.

    Legal suffixes (Ltd, Inc, GmbH, BV...) are kept — dropping them merges
    distinct entities sharing a trade name, which is worse than a near-dup.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def connect(path: Path | None = None) -> sqlite3.Connection:
    # WAL + busy_timeout so a long-running enricher and an importer can
    # coexist instead of erroring with "database is locked".
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db


def upsert_company(
    db: sqlite3.Connection,
    name: str,
    *,
    country: str | None = None,
    region: str | None = None,
    city: str | None = None,
    website: str | None = None,
    careers_url: str | None = None,
    industry: str | None = None,
    size_estimate: str | None = None,
    tier: str = "other",
) -> tuple[int, bool]:
    """Insert or enrich a company; returns (company_id, was_inserted).

    Enrichment fills NULL columns only — a field one registry reported is
    never overwritten by another source's blank.
    """
    now = utcnow()
    norm = normalize_name(name)
    cur = db.execute(
        "SELECT id FROM companies WHERE name_normalized = ? AND ifnull(country, '') = ifnull(?, '')",
        (norm, country),
    )
    row = cur.fetchone()
    if row:
        db.execute(
            """UPDATE companies SET
                   website      = ifnull(website, ?),
                   careers_url  = ifnull(careers_url, ?),
                   region       = ifnull(region, ?),
                   city         = ifnull(city, ?),
                   industry     = ifnull(industry, ?),
                   size_estimate= ifnull(size_estimate, ?),
                   tier         = CASE WHEN tier = 'other' AND ? != 'other' THEN ? ELSE tier END,
                   updated_at   = ?
               WHERE id = ?""",
            (website, careers_url, region, city, industry, size_estimate, tier, tier, now, row["id"]),
        )
        return row["id"], False
    cur = db.execute(
        """INSERT INTO companies
           (name, name_normalized, website, careers_url, country, region, city,
            industry, size_estimate, tier, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, norm, website, careers_url, country, region, city, industry,
         size_estimate, tier, now, now),
    )
    return cur.lastrowid, True


def add_sponsorship(
    db: sqlite3.Connection,
    company_id: int,
    *,
    country: str,
    program: str,
    status: str,
    source: str,
    source_detail: str | None = None,
) -> None:
    now = utcnow()
    db.execute(
        """INSERT INTO sponsorship (company_id, country, program, status, source, source_detail, first_seen, last_verified)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(company_id, country, program, source)
           DO UPDATE SET status = excluded.status,
                         source_detail = excluded.source_detail,
                         last_verified = excluded.last_verified""",
        (company_id, country, program, status, source, source_detail, now, now),
    )


def log_import(db: sqlite3.Connection, source: str, rows_in: int,
               inserted: int, matched: int, notes: str | None = None) -> None:
    db.execute(
        "INSERT INTO import_log (source, ran_at, rows_in, rows_inserted, rows_matched, notes)"
        " VALUES (?,?,?,?,?,?)",
        (source, utcnow(), rows_in, inserted, matched, notes),
    )
