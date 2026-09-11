"""Company-export import tests — offline, no spreadsheet needed.

The filtering here decides what a multi-hour probing run spends its requests
on, so the cases that matter are the ones where a bad filter wastes them: junk
placeholder names, rows with no headcount, and domains that are not domains.
"""

import pytest

from scraper.company_import import (
    CompanyRecord,
    _column_index,
    domain_root,
    is_junk,
    read_spreadsheet,
    write_names_file,
)

HEADER = "Company Name,Industry,Country,Website/ Domain,Employees\n"


def write_csv(tmp_path, rows, header=HEADER):
    path = tmp_path / "companies.csv"
    path.write_text(header + "".join(rows), encoding="utf-8")
    return path


# -- domain -> slug --------------------------------------------------------

def test_domain_root_strips_scheme_www_and_tld():
    assert domain_root("https://www.ClickUp.com/careers") == "clickup"


def test_domain_root_handles_a_bare_domain():
    assert domain_root("customtech.com") == "customtech"


def test_domain_root_handles_multi_part_tld():
    assert domain_root("www.monzo.co.uk") == "monzo"


def test_domain_root_rejects_placeholders():
    """A scraped export is full of these; each one would cost a live request."""
    for junk in ("websitehere", "example.com", "yourcompany.com", ""):
        assert domain_root(junk) is None


def test_domain_root_of_none():
    assert domain_root(None) is None


# -- junk names ------------------------------------------------------------

def test_placeholder_names_are_junk():
    for name in ("Personal", "various", "N/A", "self employed", "Unknown", "  "):
        assert is_junk(name), name


def test_real_names_are_not_junk():
    for name in ("ClickUp", "Ramp", "Hims & Hers", "3M"):
        assert not is_junk(name), name


# -- filtering -------------------------------------------------------------

def test_filters_by_country(tmp_path):
    path = write_csv(tmp_path, [
        "Acme,computer software,United States,acme.com,200\n",
        "Foreign Co,computer software,Germany,foreign.de,200\n",
    ])
    names = [r.name for r in read_spreadsheet(path)]
    assert names == ["Acme"]


def test_filters_by_industry(tmp_path):
    path = write_csv(tmp_path, [
        "Techy,computer software,United States,techy.com,200\n",
        "Hospital,hospital & health care,United States,hosp.com,200\n",
    ])
    assert [r.name for r in read_spreadsheet(path)] == ["Techy"]


def test_filters_by_headcount_band(tmp_path):
    path = write_csv(tmp_path, [
        "Tiny,computer software,United States,tiny.com,3\n",
        "JustRight,computer software,United States,jr.com,500\n",
        "Huge,computer software,United States,huge.com,90000\n",
    ])
    assert [r.name for r in read_spreadsheet(path)] == ["JustRight"]


def test_missing_headcount_is_kept_not_treated_as_zero(tmp_path):
    """Unknown is not the same as zero — dropping these loses real companies."""
    path = write_csv(tmp_path, ["NoSize,computer software,United States,nosize.com,\n"])
    assert [r.name for r in read_spreadsheet(path)] == ["NoSize"]


def test_junk_rows_are_dropped(tmp_path):
    path = write_csv(tmp_path, [
        "Personal,computer software,United States,,200\n",
        "Real Co,computer software,United States,realco.com,200\n",
    ])
    assert [r.name for r in read_spreadsheet(path)] == ["Real Co"]


def test_duplicates_are_collapsed(tmp_path):
    path = write_csv(tmp_path, [
        "Acme,computer software,United States,acme.com,200\n",
        "ACME,computer software,United States,acme.com,300\n",
    ])
    assert len(read_spreadsheet(path)) == 1


def test_largest_companies_come_first(tmp_path):
    """A run cut short should have spent its requests on the best bets."""
    path = write_csv(tmp_path, [
        "Small,computer software,United States,s.com,100\n",
        "Big,computer software,United States,b.com,2000\n",
    ])
    assert [r.name for r in read_spreadsheet(path)] == ["Big", "Small"]


def test_limit_caps_the_result(tmp_path):
    path = write_csv(tmp_path, [
        f"Co{i},computer software,United States,co{i}.com,{100 + i}\n" for i in range(10)
    ])
    assert len(read_spreadsheet(path, limit=3)) == 3


def test_empty_countries_keeps_everything(tmp_path):
    path = write_csv(tmp_path, [
        "Acme,computer software,United States,acme.com,200\n",
        "Foreign Co,computer software,Germany,foreign.de,200\n",
    ])
    assert len(read_spreadsheet(path, countries=())) == 2


# -- column matching -------------------------------------------------------

def test_columns_are_matched_by_header_not_position():
    index = _column_index(["Employees", "Company Name", "Country", "Industry", "Domain"])
    assert index["name"] == 1 and index["employees"] == 0 and index["domain"] == 4


def test_a_differently_named_export_still_works(tmp_path):
    path = write_csv(
        tmp_path,
        ["Acme,computer software,United States,acme.com,200\n"],
        header="Organization,Sector,Country,URL,Headcount\n",
    )
    records = read_spreadsheet(path)
    assert records[0].name == "Acme" and records[0].slug_hint() == "acme"


def test_missing_name_column_is_a_clear_error(tmp_path):
    path = write_csv(tmp_path, ["x,y\n"], header="Foo,Bar\n")
    with pytest.raises(ValueError, match="No company-name column"):
        read_spreadsheet(path)


def test_empty_file_returns_nothing(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    assert read_spreadsheet(path) == []


# -- output ----------------------------------------------------------------

def test_names_file_carries_the_domain(tmp_path):
    out = write_names_file(
        [CompanyRecord("Acme", "acme.com"), CompanyRecord("NoDomain")],
        tmp_path / "names.txt",
    )
    lines = [ln for ln in out.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines == ["Acme,acme.com", "NoDomain"]


def test_names_file_is_readable_by_discovery(tmp_path):
    from scraper.discovery import load_names_from_file

    out = write_names_file([CompanyRecord("Acme", "acme.com")], tmp_path / "names.txt")
    assert load_names_from_file(out) == ["Acme,acme.com"]


# -- the domain becomes the first slug guess -------------------------------

def test_domain_is_the_first_slug_candidate():
    from scraper.discovery import candidate_slugs

    slugs = candidate_slugs("Custom Computer Specialists", [], domain="customtech.com")
    assert slugs[0] == "customtech"


def test_slug_derivation_still_works_without_a_domain():
    from scraper.discovery import candidate_slugs

    assert candidate_slugs("Ramp Financial", [])[0] == "rampfinancial"
