"""Each board's authoritative inventory, from the reader to the Mac's plan (Nov 2026; devops/discovery_inventory.py).

Offline: fake clients and throwaway databases only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from data_engineering.scraper.base import CompanyRef, Listing
from data_engineering.scraper.greenhouse import GreenhouseScraper
from data_engineering.scraper.lever import LeverScraper
from data_engineering.scraper.teamtailor import parse_feed
from data_engineering.scraper.workable import WorkableScraper
from data_engineering.scraper import runner
from devops import discovery_inventory as inv
from devops import discovery_merge as dm
from qa.tests.test_discovery_partitions import (  # noqa: F401 -- `offline`: the autouse no-network fixture
    BASE_BOARDS, WORLD, healthy_world, joined, offline, opened, plan_for, run_partition, seed)

CO = CompanyRef(name="Acme", slug="acme", source="greenhouse")
T0 = datetime(2026, 11, 27, 7, 0, tzinfo=timezone.utc)


class Client:
    def __init__(self, payload=None, pages=None):
        self.payload, self.pages = payload, list(pages or [])

    def get_json(self, url, **kw):
        return self.payload

    def post(self, url, json=None):
        body = self.pages.pop(0)
        return SimpleNamespace(status_code=200, json=lambda: body)


def gh_entry(i, **over):
    return {"id": i, "title": "Data Engineer", "location": {"name": "Remote"}, "content": "x",
            "absolute_url": f"https://x/{i}", **over}


# -- the readers: every id named, and whether that is the whole board ---------------------------------------

def test_greenhouse_names_every_id_even_an_unparsable_entry_and_a_complete_listing_is_complete():
    bad = {"id": 3, "title": None, "location": 5}                     # breaks the parser, still on the board
    got = GreenhouseScraper(Client({"jobs": [gh_entry(1), gh_entry(2), bad], "meta": {"total": 3}})).fetch_jobs(CO)
    assert isinstance(got, Listing) and got.complete
    assert got.listed_ids() == ["1", "2", "3"]


@pytest.mark.parametrize("payload", [
    {"jobs": [gh_entry(1), {"title": "no id"}], "meta": {"total": 2}},    # an entry with no id
    {"jobs": [gh_entry(1)], "meta": {"total": 40}},                       # fewer than the board's own total
])
def test_greenhouse_a_malformed_or_short_listing_is_incomplete(payload):
    assert not GreenhouseScraper(Client(payload)).fetch_jobs(CO).complete


def test_greenhouse_a_payload_without_its_job_list_is_a_failed_read_not_an_empty_board():
    from data_engineering.scraper.http_client import FetchError
    with pytest.raises(FetchError):
        GreenhouseScraper(Client({"meta": {"total": 0}})).fetch_jobs(CO)


def test_lever_and_teamtailor_entries_without_an_id_make_the_listing_incomplete():
    co = CompanyRef(name="Acme", slug="acme", source="lever")
    assert not LeverScraper(Client([{"id": "a", "text": "Eng"}, {"text": "no id"}])).fetch_jobs(co).complete
    feed = ("<rss><channel><item><guid>g1</guid><title>Eng</title><link>https://x/1</link></item>"
            "<item><title>no guid</title><link>https://x/2</link></item></channel></rss>")
    got = parse_feed(feed, CompanyRef(name="A", slug="a", source="teamtailor"))
    assert not got.complete and got.listed_ids() == ["g1"]


def test_workable_pages_that_end_short_of_its_own_total_are_incomplete():
    co = CompanyRef(name="Acme", slug="acme", source="workable")
    entry = {"shortcode": "AB1", "title": "Eng", "location": {}}
    got = WorkableScraper(Client(pages=[{"results": [entry], "total": 5}])).stub_jobs(co)
    assert not got.complete
    got = WorkableScraper(Client(pages=[{"results": [entry], "total": 1}])).stub_jobs(co)
    assert got.complete and got.listed_ids() == ["AB1"]


# -- the runner: only a COMPLETE read has an inventory -------------------------------------------------------

class Board:
    def __init__(self, listing, details=None):
        self.listing, self.details = listing, details

    def fetch_jobs(self, company):
        return self.listing


def _fetched(listing):
    return runner._fetch(Board(listing), CO, SimpleNamespace(matches_stub=lambda j: True))


def test_the_runner_judges_closure_only_from_a_complete_listing():
    from data_engineering.scraper.base import RawJob
    job = RawJob(source="greenhouse", company="A", company_slug="acme", external_id="1", title="t",
                 location=None, description="d", application_url="u")
    assert _fetched(Listing([job], ids=["1", "2"])).listed_ids == ["1", "2"]
    incomplete = _fetched(Listing([job], ids=["1"], malformed=1))
    assert incomplete.listed_ids is None and incomplete.detail["listing"] == "incomplete"
    assert _fetched([job]).listed_ids == ["1"]                          # a plain list: an older reader, complete


def test_a_partition_hands_over_an_inventory_for_its_complete_boards_only(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    WORLD["beta"] = 500                                                   # a failed read: no inventory
    plan = plan_for(base)
    pid = next(p["id"] for p in plan["partitions"] if any(b["slug"] == "acme" for b in p["boards"]))
    part = run_partition(tmp_path, base, plan, pid)
    with opened(part) as c:
        rows = {(s, slug): (rid, listed, dig, inv.unpack(blob)) for s, slug, rid, _at, listed, dig, blob in
                c.execute("SELECT * FROM inventory")}
        status = dict(((s, slug), st) for s, slug, st in c.execute("SELECT source, slug, status FROM boards"))
    assert set(rows) == {k for k, st in status.items() if st == "complete"}
    assert ("greenhouse", "beta") not in rows
    rid, listed, dig, ids = rows[("greenhouse", "acme")]
    assert rid == f"R-1-{pid}" and sorted(ids) == ["acme-1", "acme-2"] and listed == 2 and dig == inv.digest(ids)


# -- the join: sound inventories only, the previous one kept, never older over newer -------------------------

def _join(tmp_path, base, plan, parts, name="join.db"):
    j = joined(tmp_path, base, name)
    dm.merge(j, plan, parts)
    return j


def _saved(db):
    with opened(db) as c:
        return {(s, slug): r for s, slug, *r in c.execute(
            "SELECT source, slug, run_id, listed, prev_run_id, prev_listed FROM board_inventory")}


def test_the_join_keeps_the_newest_inventory_with_the_previous_beside_it(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    first = {p["id"]: run_partition(tmp_path, base, plan, p["id"], attempt="1") for p in plan["partitions"]}
    j = _join(tmp_path, base, plan, first)
    once = _saved(j)
    assert once[("greenhouse", "acme")][1] == 2 and once[("greenhouse", "acme")][2] is None
    dm.merge(j, plan, first)                                              # the same read twice: no change
    assert _saved(j) == once
    WORLD["acme"] = ["acme-1"]
    second = {p["id"]: run_partition(tmp_path, j, plan, p["id"], attempt="2") for p in plan["partitions"]}
    dm.merge(j, plan, second)
    now = _saved(j)[("greenhouse", "acme")]
    assert now[0].startswith("R-2-") and now[1] == 1 and now[2].startswith("R-1-") and now[3] == 2
    dm.merge(j, plan, first)                                              # an older read never over a newer
    assert _saved(j)[("greenhouse", "acme")] == now


def test_an_unsound_inventory_is_never_taken_and_the_boards_postings_still_are(tmp_path):
    base = seed(tmp_path / "base.db", BASE_BOARDS)
    healthy_world()
    plan = plan_for(base)
    parts = {p["id"]: run_partition(tmp_path, base, plan, p["id"]) for p in plan["partitions"]}
    pid = next(p for p in parts if p.startswith("greenhouse") and
               any(b["slug"] == "acme" for b in next(x for x in plan["partitions"] if x["id"] == p)["boards"]))
    with opened(parts[pid]) as c:
        c.execute("UPDATE inventory SET digest = 'x' WHERE slug = 'acme'")      # tampered
    j = _join(tmp_path, base, plan, parts)
    assert ("greenhouse", "acme") not in _saved(j)
    with opened(j) as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE external_id = 'acme-2'").fetchone()[0] == 1


# -- the Mac: load, plan, apply ------------------------------------------------------------------------------

def mac_db(tmp_path, rows):
    """rows: (external_id, is_open, status, last_seen, closed_at, applied_at) on greenhouse/acme."""
    db = tmp_path / "mac.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, company_slug TEXT, external_id TEXT, "
              "is_open INTEGER, status TEXT, last_seen_at TEXT, closed_at TEXT, applied_at TEXT, "
              "exported_to_excel INTEGER)")
    for ext, is_open, status, seen, closed, applied in rows:
        c.execute("INSERT INTO jobs (source, company_slug, external_id, is_open, status, last_seen_at, closed_at, "
                  "applied_at, exported_to_excel) VALUES ('greenhouse', 'acme', ?, ?, ?, ?, ?, ?, 1)",
                  (ext, is_open, status, seen, closed, applied))
    c.commit()
    return c


OLD_SEEN = "2026-10-01 00:00:00.000000"


def one(ids, prev=None):
    return {("greenhouse", "acme"): inv.Inventory("greenhouse", "acme", "900-1-greenhouse-1of3", T0, set(ids),
                                                  None if prev is None else set(prev),
                                                  None if prev is None else T0 - timedelta(hours=10))}


def state(c):
    return {r[0]: r[1:] for r in c.execute("SELECT external_id, is_open, status, closed_at FROM jobs")}


def test_a_posting_the_complete_inventory_no_longer_names_closes_keeping_a_persons_status(tmp_path):
    c = mac_db(tmp_path, [("live", 1, "Not Applied", OLD_SEEN, None, None),
                          ("gone", 1, "Manual Review", OLD_SEEN, None, None),
                          ("applied-gone", 1, "Applied", OLD_SEEN, None, "2026-10-02"),
                          ("rejected-gone", 1, "Rejected", OLD_SEEN, None, None)])
    p = inv.plan(c, one(["live"] + [f"other-{i}" for i in range(20)]), set())
    assert {r[1][2] for r in p.close} == {"gone", "applied-gone", "rejected-gone"} and not p.held
    rep = inv.report(p, {}, one(["live"]))
    assert {r["status"] for r in rep["acted_on_closing"]} == {"Applied", "Rejected"}
    inv.apply(c, p)
    s = state(c)
    assert s["live"][0] == 1
    assert s["gone"][:2] == (0, "Closed")
    assert s["applied-gone"][:2] == (0, "Applied") and s["rejected-gone"][:2] == (0, "Rejected")
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4            # nothing deleted
    again = inv.plan(c, one(["live"] + [f"other-{i}" for i in range(20)]), set())
    assert not again.close and not again.reopen and not again.refresh         # applied twice: nothing more


def test_a_posting_seen_after_the_inventory_was_read_is_never_closed_by_it(tmp_path):
    later = (T0 + timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ")
    c = mac_db(tmp_path, [("seen-later", 1, "Not Applied", later, None, None)])
    p = inv.plan(c, one(["x"] * 1 + [f"o{i}" for i in range(5)]), set())
    assert not p.close and p.newer_kept == 1


def test_the_clouds_own_copies_are_left_to_the_import(tmp_path):
    c = mac_db(tmp_path, [("held-there", 1, "Not Applied", OLD_SEEN, None, None)])
    assert not inv.plan(c, one([f"o{i}" for i in range(5)]), {("greenhouse", "acme", "held-there")}).close


@pytest.mark.parametrize("ids,prev,why", [
    ([], None, "empty"),
    ([f"o{i}" for i in range(4)], [f"o{i}" for i in range(20)] + [f"g{i}" for i in range(12)], "shrank"),
    (["live"], None, "more than half"),
])
def test_a_suspicious_inventory_holds_its_closures_until_a_second_read_agrees(tmp_path, ids, prev, why):
    c = mac_db(tmp_path, [("live", 1, "Not Applied", OLD_SEEN, None, None)] +
               [(f"g{i}", 1, "Not Applied", OLD_SEEN, None, None) for i in range(12)])
    p = inv.plan(c, one(ids, prev), set())
    held = {r[1][2] for r in p.held}
    assert held >= {f"g{i}" for i in range(12)} and why in p.boards[("greenhouse", "acme")]["suspect"]
    # Closed at once only what the previous read did not name either ("live" in the shrink case).
    assert {r[1][2] for r in p.close} <= ({"live"} - held if prev and "live" not in prev else set())
    # A second, independent complete read that also does not name them: now they close.
    confirmed = inv.plan(c, one(ids, ids), set())
    assert {r[1][2] for r in confirmed.close} >= {f"g{i}" for i in range(12)}


def test_a_closed_posting_the_inventory_names_again_reopens_and_a_listed_one_is_refreshed(tmp_path):
    c = mac_db(tmp_path, [("back", 0, "Closed", OLD_SEEN, "2026-11-01 00:00:00", None),
                          ("back-applied", 0, "Applied", OLD_SEEN, "2026-11-01 00:00:00", "2026-10-02"),
                          ("listed", 1, "Not Applied", OLD_SEEN, None, None)])
    p = inv.plan(c, one(["back", "back-applied", "listed"]), set())
    inv.apply(c, p)
    s = state(c)
    assert s["back"][:2] == (1, "Not Applied") and s["back-applied"][:2] == (1, "Applied") and s["listed"][0] == 1
    assert c.execute("SELECT last_seen_at FROM jobs WHERE external_id = 'listed'").fetchone()[0] > OLD_SEEN


def _saved_db(tmp_path, rows):
    db = tmp_path / "cloud.db"
    c = sqlite3.connect(db)
    c.execute(inv.SAVED_SCHEMA)
    c.executemany(f"INSERT INTO {inv.SAVED_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    c.commit()
    c.close()
    return db


def _row(rid="900-1-greenhouse-1of3", at=T0, ids=("a", "b"), listed=None, dig=None, slug="acme", prev=None):
    ids = list(ids)
    p = (None,) * 5 if prev is None else (prev[0], prev[1].isoformat(), len(prev[2]), inv.digest(prev[2]),
                                          inv.pack(prev[2]))
    return ("greenhouse", slug, rid, at.isoformat(), len(ids) if listed is None else listed,
            dig or inv.digest(ids), inv.pack(ids), *p)


RESULT = {"run": {"id": "900", "started_at": (T0 - timedelta(hours=1)).isoformat(),
                  "finished_at": (T0 + timedelta(hours=1)).isoformat()},
          "boards": [["greenhouse", "acme", "complete", "", "greenhouse-1of3"],
                     ["greenhouse", "beta", "partial", "", "greenhouse-1of3"]]}
CHECKED = {"scanned": {("greenhouse", "acme"), ("greenhouse", "beta")}}


@pytest.mark.parametrize("row,why", [
    (_row(rid="899-1-greenhouse-1of3"), None),                              # an earlier run's: silently not this one
    (_row(slug="beta"), "not read completely"),                             # partial board
    (_row(rid="900-1-greenhouse-2of3"), "own partition"),
    (_row(at=T0 - timedelta(hours=3)), "inside this run"),
    (_row(at=T0 + timedelta(hours=3)), "inside this run"),
    (_row(listed=5), "count or digest"),
    (_row(dig="0" * 64), "count or digest"),
])
def test_the_mac_takes_an_inventory_only_from_this_runs_complete_read_and_intact(tmp_path, row, why):
    got, refused = inv.load(_saved_db(tmp_path, [row]), RESULT, CHECKED)
    assert not got
    assert (not refused) if why is None else any(why in k for k in refused)


def test_the_mac_takes_a_sound_inventory_and_its_independent_previous_read(tmp_path):
    good = _row(prev=("899-1-greenhouse-1of3", T0 - timedelta(hours=10), ["a", "b", "c"]))
    got, refused = inv.load(_saved_db(tmp_path, [good]), RESULT, CHECKED)
    i = got[("greenhouse", "acme")]
    assert i.ids == {"a", "b"} and i.prev_ids == {"a", "b", "c"} and not refused


def test_a_result_from_before_inventories_reconciles_nothing(tmp_path):
    db = tmp_path / "old.db"
    sqlite3.connect(db).execute("CREATE TABLE jobs (id INTEGER)").connection.close()
    assert inv.load(db, RESULT, CHECKED) == ({}, {})
