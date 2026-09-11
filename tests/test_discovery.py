"""Discovery tests — in-memory DB, no network.

Discovery is the one part of this tool that sends a request per GUESS rather
than per known company, so a 182-name list is ~700 requests. The probe cache is
therefore a politeness feature as much as a speed one (README.md §C5), and most
of what is tested here is that it actually prevents repeat requests.
"""

import pytest

from db import session as session_mod
from db.models import Company, ProbeLog
from db.session import get_session, init_engine
from scraper.discovery import (
    CompanyDiscoverer,
    candidate_slugs,
    import_inventory_csv,
    load_probe_cache,
    record_probe,
)


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


class CountingScraper:
    """Records every board_exists call so repeat probes are visible."""

    def __init__(self, name, live_slugs=()):
        self.name = name
        self.live = set(live_slugs)
        self.calls: list[str] = []

    def board_exists(self, slug):
        self.calls.append(slug)
        return slug in self.live

    def board_url(self, slug):
        return f"https://{self.name}.example/{slug}"


def discoverer_with(**scrapers):
    return CompanyDiscoverer(scrapers=scrapers, strip_suffixes=["inc", "corp", "labs"])


# -- slug derivation -------------------------------------------------------

def test_candidate_slugs_strips_suffixes():
    assert candidate_slugs("Ramp Financial, Inc.", ["inc"]) == ["rampfinancial", "ramp-financial", "ramp"]


def test_candidate_slugs_handles_ampersand():
    assert "himsandhers" in candidate_slugs("Hims & Hers")


def test_candidate_slugs_of_an_empty_name():
    assert candidate_slugs("Inc.", ["inc"]) == []


# -- the probe cache -------------------------------------------------------

def test_probe_outcomes_are_recorded(db):
    gh = CountingScraper("greenhouse", ["ramp"])
    discoverer_with(greenhouse=gh).discover_from_names(["Ramp"], ["greenhouse"])

    with get_session() as s:
        row = s.query(ProbeLog).filter_by(source="greenhouse", slug="ramp").one()
        assert row.found is True
        assert row.company_name == "Ramp"


def test_misses_are_recorded_too(db):
    """A dead slug that is not remembered gets re-asked on every future run."""
    gh = CountingScraper("greenhouse", [])
    discoverer_with(greenhouse=gh).discover_from_names(["Nope"], ["greenhouse"])

    with get_session() as s:
        assert s.query(ProbeLog).filter_by(found=False).count() >= 1


def test_a_cached_miss_is_never_re_requested(db):
    gh = CountingScraper("greenhouse", [])
    d = discoverer_with(greenhouse=gh)

    d.discover_from_names(["Nope"], ["greenhouse"])
    first_round = len(gh.calls)
    assert first_round > 0

    d.discover_from_names(["Nope"], ["greenhouse"])
    assert len(gh.calls) == first_round  # nothing new went out
    assert d.progress.probes_sent == 0
    assert d.progress.probes_skipped > 0


def test_a_cached_hit_is_returned_without_a_request(db):
    gh = CountingScraper("greenhouse", ["ramp"])
    d = discoverer_with(greenhouse=gh)

    d.discover_from_names(["Ramp"], ["greenhouse"])
    calls_after_first = len(gh.calls)

    results = d.discover_from_names(["Ramp"], ["greenhouse"])
    assert results[0].found is True
    assert len(gh.calls) == calls_after_first


def test_record_probe_is_idempotent(db):
    record_probe("greenhouse", "ramp", False)
    record_probe("greenhouse", "ramp", True, company_name="Ramp")

    with get_session() as s:
        row = s.query(ProbeLog).filter_by(source="greenhouse", slug="ramp").one()
        assert row.found is True  # the later answer wins
    assert load_probe_cache()[("greenhouse", "ramp")] is True


def test_findings_persist_as_they_happen(db):
    """A run interrupted after N probes must not throw away what they cost."""
    gh = CountingScraper("greenhouse", ["b"])

    class Boom(Exception):
        pass

    def explode(progress, result):
        if progress.names == 2:
            raise Boom

    with pytest.raises(Boom):
        discoverer_with(greenhouse=gh).discover_from_names(["a", "b", "c"], ["greenhouse"], on_progress=explode)

    with get_session() as s:
        assert s.query(ProbeLog).count() > 0
        assert s.query(Company).filter_by(slug="b").count() == 1


# -- probing behaviour -----------------------------------------------------

def test_first_hit_wins_across_sources(db):
    gh = CountingScraper("greenhouse", [])
    lever = CountingScraper("lever", ["acme"])
    result = discoverer_with(greenhouse=gh, lever=lever).discover_from_names(
        ["Acme"], ["greenhouse", "lever"]
    )[0]
    assert (result.source, result.slug) == ("lever", "acme")


def test_a_found_company_is_saved(db):
    ashby = CountingScraper("ashby", ["cedar"])
    discoverer_with(ashby=ashby).discover_from_names(["Cedar"], ["ashby"])

    with get_session() as s:
        row = s.query(Company).filter_by(source="ashby", slug="cedar").one()
        assert row.origin == "discovery"
        assert row.board_url == "https://ashby.example/cedar"


def test_a_probe_error_does_not_stop_the_run(db):
    class Exploding(CountingScraper):
        def board_exists(self, slug):
            raise RuntimeError("network gone")

    results = discoverer_with(greenhouse=Exploding("greenhouse")).discover_from_names(
        ["A", "B"], ["greenhouse"]
    )
    assert len(results) == 2 and not any(r.found for r in results)


def test_progress_counts_names_and_finds(db):
    gh = CountingScraper("greenhouse", ["a"])
    d = discoverer_with(greenhouse=gh)
    d.discover_from_names(["A", "B"], ["greenhouse"])
    assert d.progress.names == 2
    assert d.progress.found == 1


def test_plan_lists_probes_without_sending_any(db):
    """--dry-run must cost nothing."""
    gh = CountingScraper("greenhouse", ["ramp"])
    plan = discoverer_with(greenhouse=gh).plan("Ramp Inc", ["greenhouse"])
    assert ("greenhouse", "ramp") in plan
    assert gh.calls == []


# -- CSV import ------------------------------------------------------------

def test_csv_import_accepts_ashby(db, tmp_path):
    """Ashby was rejected by the source allow-list until it was supported."""
    path = tmp_path / "inv.csv"
    path.write_text("name,slug,source\nCedar,cedar,ashby\nRamp,ramp,greenhouse\n")
    assert import_inventory_csv(path) == 2

    with get_session() as s:
        assert s.query(Company).filter_by(source="ashby", slug="cedar").count() == 1


def test_csv_import_skips_unknown_sources(db, tmp_path):
    path = tmp_path / "inv.csv"
    path.write_text("name,slug,source\nBig Co,bigco,workday\n")
    assert import_inventory_csv(path) == 0
