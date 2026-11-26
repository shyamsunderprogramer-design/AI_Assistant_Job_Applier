"""Workable's official job feed (26 Nov 2026): one compressed file, parsed safely as a stream, added beside the
Workable boards -- never in place of them, never a duplicate of their postings, never closing anything of theirs."""

from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_engineering.scraper import workable_feed as wf
from data_engineering.scraper.filters import JobFilter


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def job(code, title="Site Reliability Engineer", company="Acme", city="Austin", state="TX", country="US",
        remote="false", description="<p>Kubernetes and <b>Terraform</b></p>"):
    return (f"<job><title><![CDATA[{title}]]></title><date><![CDATA[Wed, 25 Nov 2026 10:00:00 UTC]]></date>"
            f"<referencenumber><![CDATA[{code}]]></referencenumber>"
            f"<url><![CDATA[https://apply.workable.com/j/{code}]]></url><company><![CDATA[{company}]]></company>"
            f"<city><![CDATA[{city}]]></city><state><![CDATA[{state}]]></state><country><![CDATA[{country}]]></country>"
            f"<remote><![CDATA[{remote}]]></remote><description><![CDATA[{description}]]></description>"
            f"<jobtype><![CDATA[Full-time]]></jobtype></job>")


def feed_file(path: Path, *jobs: str, head: str = "") -> Path:
    body = f'<?xml version="1.0" encoding="UTF-8"?>{head}<source><publisher>Workable</publisher>{"".join(jobs)}</source>'
    with gzip.open(path, "wb") as fh:
        fh.write(body.encode())
    return path


def test_the_feed_is_read_as_a_stream_keeping_only_the_wanted_postings_and_every_shortcode(tmp_path):
    f = feed_file(tmp_path / "f.xml.gz", job("AAA111"), job("BBB222", title="Accountant"), job("AAA111"),
                  job("CCC333", remote="true", city="", state="", country=""))
    kept, codes = wf.parse(f, lambda j: "Engineer" in j.get("title", ""))
    assert codes == {"AAA111", "BBB222", "CCC333"}
    assert [j["referencenumber"] for j in kept] == ["AAA111", "AAA111", "CCC333"]
    assert wf.location(kept[0]) == "Austin, TX, US" and wf.location(kept[2]) == "Remote"
    raw = wf.raw_job(kept[0])
    assert raw is not None and raw.source == "workable-feed" and raw.external_id == "AAA111"
    assert raw.application_url == "https://apply.workable.com/j/AAA111"      # exactly as given: attribution
    assert raw.description == "Kubernetes and Terraform" and raw.job_type == "Full-time"
    assert raw.posted_at is not None and raw.posted_at.year == 2026


@pytest.mark.parametrize("head", [
    '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>',
    '<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>',
    '<!DOCTYPE source SYSTEM "http://example.com/evil.dtd">',
])
def test_a_doctype_or_an_entity_is_refused_never_expanded_or_fetched(tmp_path, head):
    f = feed_file(tmp_path / "f.xml.gz", job("AAA111"), head=head)
    with pytest.raises(wf.Refused):
        wf.parse(f, lambda j: True)


def test_a_file_cut_short_is_not_a_feed(tmp_path):
    whole = feed_file(tmp_path / "f.xml.gz", *(job(f"C{i:05d}") for i in range(200)))
    cut = tmp_path / "cut.xml.gz"
    cut.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])
    with pytest.raises((EOFError, wf.expat.ExpatError)):
        wf.parse(cut, lambda j: True)


class Resp:
    def __init__(self, status=200, body=b"", encoding="gzip"):
        self.status_code, self.headers = status, ({"Content-Encoding": encoding} if encoding else {})
        self._body = body

        class Raw:
            def stream(_self, n, decode_content=False):
                assert decode_content is False          # kept compressed on disk
                for i in range(0, len(body), n):
                    yield body[i:i + n]
        self.raw = Raw()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Session:
    def __init__(self, resp):
        self.resp, self.calls, self.headers = resp, 0, None

    def get(self, url, headers=None, **kw):
        self.calls += 1
        self.headers = headers
        assert url == wf.URL and kw.get("stream") is True and kw.get("timeout")
        return self.resp


@pytest.mark.parametrize("resp, why", [(Resp(429), "HTTP 429"), (Resp(403), "HTTP 403"),
                                       (Resp(200, b"<x/>", encoding=""), "compressed")])
def test_a_refusal_is_reported_once_and_never_retried(tmp_path, resp, why):
    s = Session(resp)
    with pytest.raises(RuntimeError, match=why):
        wf.download(tmp_path / "f.gz", session=s)
    assert s.calls == 1 and s.headers["Accept-Encoding"] == "gzip"


def test_a_file_far_larger_than_the_feed_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "MAX_BYTES", 10)
    with pytest.raises(RuntimeError, match="larger"):
        wf.download(tmp_path / "f.gz", session=Session(Resp(200, b"x" * (3 << 20))))


# -- storing: beside the boards, never over them -----------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    from data_engineering.db import session as session_mod
    from data_engineering.db.session import init_engine
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setattr("data_engineering.scraper.filters.resolve_filter",
                        lambda cfg: JobFilter(title_keywords=["engineer"]))
    monkeypatch.setattr(wf, "MIN_JOBS", 1)
    yield tmp_path / "jobs.db"
    session_mod._engine = None
    session_mod._SessionFactory = None


def add(source, slug, code, title="Site Reliability Engineer", company="Acme", seen=None, is_open=True,
        status="Not Applied"):
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    with get_session() as s:
        s.add(Job(source=source, company=company, company_slug=slug, external_id=code, title=title,
                  application_url="https://example.com", content_hash=code, status=status, is_open=is_open,
                  last_seen_at=seen or _utcnow()))


def rows(db):
    con = sqlite3.connect(db)
    try:
        return sorted(con.execute("SELECT source, external_id, is_open, status FROM jobs"))
    finally:
        con.close()


class Cfg:
    def get(self, key, default=None):
        return default


def run_with(db, *jobs):
    src = feed_file(db.parent / "src.xml.gz", *jobs)
    return wf.run(Cfg(), session=Session(Resp(200, src.read_bytes())), workdir=db.parent)


def test_new_postings_are_added_under_their_own_source_and_the_boards_postings_are_never_added_again(db):
    add("workable", "acme", "BRD001")                                  # a Workable board's posting
    add("greenhouse", "beta", "g1", company="Beta", title="Platform Engineer")
    s = run_with(db, job("BRD001"), job("NEW001", company="Gamma"), job("NEW001", company="Gamma"),
                 job("DUP001", company="Beta", title="Platform Engineer"), job("OTH001", title="Accountant"))
    assert s.status == "read" and s.complete and s.jobs == 4
    assert (s.new, s.on_boards, s.duplicates) == (1, 1, 1)
    assert rows(db) == [("greenhouse", "g1", 1, "Not Applied"), ("workable", "BRD001", 1, "Not Applied"),
                        ("workable-feed", "NEW001", 1, "Not Applied")]
    again = run_with(db, job("BRD001"), job("NEW001", company="Gamma"))
    assert (again.new, again.seen) == (0, 1)


def test_only_a_complete_copy_closes_and_only_postings_missing_for_days_and_only_its_own(db):
    old = _utcnow() - timedelta(days=wf.CLOSE_AFTER_DAYS + 1)
    add("workable-feed", "acme", "OLD001", seen=old)
    add("workable", "acme", "BRD001", seen=old)                        # a board's: never the feed's to close
    add("workable-feed", "acme", "NEW001", company="Acme", title="Cloud Engineer")   # seen today
    s = run_with(db, job("KEEP01", company="Zeta"))
    # one recent posting of its own missing (under 20 of them): the copy counts as complete
    assert s.complete and s.retired == 1
    assert ("workable-feed", "OLD001", 0, "Not Applied") in rows(db)
    assert ("workable", "BRD001", 1, "Not Applied") in rows(db)
    assert ("workable-feed", "NEW001", 1, "Not Applied") in rows(db)   # missing a day: kept


def test_a_copy_missing_most_recent_postings_or_far_too_small_closes_nothing(db, monkeypatch):
    for i in range(25):
        add("workable-feed", "acme", f"R{i:04d}", title=f"Engineer {i}")
    old = _utcnow() - timedelta(days=10)
    add("workable-feed", "acme", "OLD001", seen=old, title="Old Engineer")
    s = run_with(db, job("R0000"), job("R0001"))
    assert s.status == "read" and not s.complete and s.retired == 0 and "nothing is closed" in s.why
    assert ("workable-feed", "OLD001", 1, "Not Applied") in rows(db)
    assert wf.complete_copy(5, {"a"}, set()) == (True, "")
    monkeypatch.setattr(wf, "MIN_JOBS", 20_000)
    assert wf.complete_copy(5, {"a"}, set())[0] is False


def test_a_failed_download_or_parse_stores_and_closes_nothing(db):
    old = _utcnow() - timedelta(days=10)
    add("workable-feed", "acme", "OLD001", seen=old)
    before = rows(db)
    s = wf.run(Cfg(), session=Session(Resp(429)), workdir=db.parent)
    assert s.status == "failed" and "429" in s.why and rows(db) == before
    bad = db.parent / "bad.xml.gz"
    feed_file(bad, job("A"), head='<!DOCTYPE x [<!ENTITY a "b">]>')
    s = wf.run(Cfg(), session=Session(Resp(200, bad.read_bytes())), workdir=db.parent)
    assert s.status == "failed" and s.why == "parse: Refused" and rows(db) == before
    assert not list(db.parent.glob("tmp*/workable.xml.gz"))            # the download is never left behind


def test_the_join_never_adds_a_feed_copy_of_a_boards_posting_and_closes_one_a_board_now_holds(tmp_path):
    from devops import discovery_merge as dm
    schema = ("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, company_slug TEXT, external_id TEXT, "
              "title TEXT, is_open INTEGER, closed_at TEXT, UNIQUE (source, company_slug, external_id))")
    main, feeds = tmp_path / "jobs.db", tmp_path / "feeds"
    feeds.mkdir()
    con = sqlite3.connect(main)
    con.execute(schema)
    con.execute("CREATE TABLE companies (source TEXT, slug TEXT)")
    con.execute("INSERT INTO companies VALUES ('workable', 'acme')")
    con.executemany("INSERT INTO jobs (source, company_slug, external_id, title, is_open) VALUES (?, ?, ?, ?, 1)",
                    [("workable", "acme", "BRD001", "SRE"), ("workable", "acme", "BRD002", "SRE 2"),
                     ("workable-feed", "acme", "BRD002", "SRE 2"), ("workable-feed", "beta", "OWN001", "DevOps")])
    con.commit()
    con.close()
    con = sqlite3.connect(feeds / "feeds.db")
    con.execute(schema)
    con.executemany("INSERT INTO jobs (source, company_slug, external_id, title, is_open) VALUES (?, ?, ?, ?, 1)",
                    [("workable-feed", "acme", "BRD001", "SRE"), ("workable-feed", "gamma", "NEW001", "SRE 3")])
    con.commit()
    con.close()
    (feeds / "feeds-report.json").write_text('{"read": ["workable-feed"], "row_sources": ["workable-feed"]}')
    got = dm.merge_feeds(main, feeds)
    assert (got["added"], got["on_boards"], got["superseded"]) == (1, 1, 1)
    con = sqlite3.connect(main)
    have = sorted(con.execute("SELECT source, external_id, is_open FROM jobs"))
    con.close()
    assert have == [("workable", "BRD001", 1), ("workable", "BRD002", 1), ("workable-feed", "BRD002", 0),
                    ("workable-feed", "NEW001", 1), ("workable-feed", "OWN001", 1)]
