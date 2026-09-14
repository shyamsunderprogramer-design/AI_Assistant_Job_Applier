"""Importing campus careers pages as boards — offline, no network.

Everything pinned here is a way the directory's own data disagrees with the
assumption "one school, one board", which is the assumption that would have
produced duplicate scrapes and mislabelled companies.
"""

from universities.import_boards import (
    Board,
    acronyms,
    collect,
    domain_root,
    plausible,
    slug_for,
    unsupported_tally,
)


def row(name, ats, url, domain="", unitid="1"):
    return {"name": name, "jobs_page_ats": ats, "jobs_page_url": url,
            "domain": domain, "unitid": unitid}


# -- recovering a slug from a careers URL ----------------------------------

def test_a_workday_slug_carries_all_three_parts():
    assert slug_for("workday", "https://njit.wd108.myworkdayjobs.com/NJIT_Careers") == \
        "njit:wd108:NJIT_Careers"


def test_greenhouse_lever_and_ashby_slugs():
    assert slug_for("greenhouse", "https://boards.greenhouse.io/centuracollege") == \
        "centuracollege"
    assert slug_for("lever", "https://jobs.lever.co/envato-2") == "envato-2"
    assert slug_for("ashby", "https://jobs.ashbyhq.com/Campus") == "campus"


def test_a_careers_page_that_is_not_an_ats_yields_nothing():
    assert slug_for("greenhouse", "https://www.akbible.edu/employment") is None


# -- a board is not a school ------------------------------------------------

def test_campuses_sharing_a_board_are_collapsed():
    """Four University of Wisconsin campuses post to one Workday site.

    Importing each separately would scrape the same postings four times and
    file them under four different employers.
    """
    url = "https://wisconsin.wd1.myworkdayjobs.com/UW_Comprehensives"
    boards, _ = collect([
        row("University of Wisconsin-La Crosse", "Workday", url),
        row("University of Wisconsin-Parkside", "Workday", url),
        row("University of Wisconsin-Platteville", "Workday", url),
    ])

    assert len(boards) == 1
    assert len(boards[0].schools) == 3


def test_a_shared_board_is_named_for_what_the_campuses_share():
    board = Board(source="workday", slug="wisconsin:wd1:UW",
                  schools=["University of Wisconsin-La Crosse",
                           "University of Wisconsin-Parkside",
                           "University of Wisconsin-Platteville"])
    assert board.name == "University of Wisconsin"


def test_a_single_campus_board_keeps_its_own_name():
    assert Board(source="workday", slug="njit:wd108:X",
                 schools=["New Jersey Institute of Technology"]).name == \
        "New Jersey Institute of Technology"


def test_a_shared_name_that_says_nothing_falls_back():
    """"University of" alone names no institution."""
    board = Board(source="workday", slug="x:wd1:y",
                  schools=["University of Baltimore", "University of Maryland"])
    assert board.name == "University of Maryland"     # shortest, not "University of"


# -- resemblance: a warning, never a filter ---------------------------------

def test_a_school_is_recognised_by_its_own_domain():
    assert plausible("New Jersey Institute of Technology", "njit:wd108:X", "njit.edu")


def test_a_school_is_recognised_by_its_initials():
    """Schools overwhelmingly name boards after themselves this way. A check
    that only compared whole words threw away 20 real boards."""
    assert "njit" in acronyms("New Jersey Institute of Technology")
    assert plausible("Northern Kentucky University", "nku:wd108:NKU_External")
    assert plausible("University of North Florida", "unf:wd5:unfjobs")
    assert plausible("Saint Joseph's University", "sju:wd1:sju")


def test_an_unrelated_slug_is_flagged_but_not_dropped():
    """`jobs.lever.co/envato-2` is listed under a barbering academy, and Envato
    is an Australian marketplace — the board is real and answers, so liveness
    proves nothing. Resemblance is the only guard, and it is only a warning:
    the dominant case for a slug that does not resemble its school is a state
    system, not a mistake."""
    assert not plausible("Kenny's Academy of Barbering", "envato-2", "kennysacademy.edu")

    boards, dropped = collect([
        row("Kenny's Academy of Barbering", "Lever",
            "https://jobs.lever.co/envato-2", "kennysacademy.edu")])

    assert dropped == []
    assert len(boards) == 1
    assert boards[0].doubtful


def test_a_system_board_is_not_doubtful_however_it_is_named():
    """Several schools pointing at one slug is a system board by definition —
    a misattribution is not shared."""
    url = "https://uasys.wd5.myworkdayjobs.com/UASYS"
    boards, _ = collect([
        row("University of Arkansas at Little Rock", "Workday", url, "ualr.edu"),
        row("University of Arkansas at Pine Bluff", "Workday", url, "uapb.edu"),
    ])
    assert not boards[0].doubtful


def test_domain_root():
    assert domain_root("https://www.njit.edu/") == "njit"
    assert domain_root("akbible.edu") == "akbible"
    assert domain_root("") == ""


# -- what still needs a scraper --------------------------------------------

def test_unsupported_platforms_are_counted_not_silently_dropped():
    tally = dict(unsupported_tally([
        row("A", "NEOGOV", "https://x.schooljobs.com/careers/a"),
        row("B", "NEOGOV", "https://x.schooljobs.com/careers/b"),
        row("C", "PeopleAdmin", "https://c.peopleadmin.com/postings"),
        row("D", "Workday", "https://d.wd1.myworkdayjobs.com/D"),
    ]))
    assert tally == {"NEOGOV": 2, "PeopleAdmin": 1}


def test_an_unsupported_platform_produces_no_board():
    boards, dropped = collect([row("A", "Paycom", "https://x.paycomonline.net/a")])
    assert boards == [] and dropped == []
