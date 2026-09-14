"""The search must come from the resume — whatever field the resume is in.

Every test here corresponds to a way this tool used to hand someone else's
job search to a person who never asked for it. The bug was invisible: the
run succeeded, the table filled with jobs, and nothing said the search was
never theirs.
"""

from resume.parser import parse_text
from resume.profile import (
    MIN_BODY_EVIDENCE,
    build_exclusions,
    build_title_keywords,
    derive_search_profile,
    extract_years_experience,
    match_families,
)


ELECTRICAL = """SHYAM R
Fayetteville, AR, USA | ee@example.com

SUMMARY
Electrical Engineer with 9 years in power systems design and protection.
Substation design, switchgear specification, relay coordination, ETAP and
AutoCAD Electrical. Familiar with networking of protection IEDs over the
station LAN.

EXPERIENCE
Senior Electrical Engineer
Burns Engineering, Fayetteville, AR | Jun 2019 - Present
- Designed 138kV substation layouts and one-line diagrams

Electrical Engineer
Delta Power, Little Rock, AR | Jul 2016 - May 2019
"""

NURSE = """MARIA L
Columbus, Ohio, USA | nurse@example.com

SUMMARY
Registered Nurse with 6 years as a staff nurse in acute care. BSN, ACLS and
BLS certified. Epic documentation, triage, IV therapy and patient care.

EXPERIENCE
Staff Nurse
Riverside Medical Center, Columbus, OH | Mar 2020 - Present

Charge Nurse
St Annes Hospital, Columbus, OH | Jan 2018 - Feb 2020
"""

ACCOUNTANT = """DEV P
Boston, MA, USA | acct@example.com

SUMMARY
Staff Accountant with 5 years in general ledger and month-end close. GAAP,
reconciliation, accounts payable, accounts receivable, audit, QuickBooks.

EXPERIENCE
Staff Accountant
Harbor Group, Boston, MA | Feb 2021 - Present
"""


def profile_of(text):
    return derive_search_profile(parse_text(text))


# -- the search belongs to the person it was derived from -------------------

def test_an_electrical_resume_searches_for_electrical_work():
    profile = profile_of(ELECTRICAL)
    joined = " ".join(profile.titles)

    assert "electrical engineer" in joined
    assert "python" not in joined
    assert "software engineer" not in joined
    assert profile.derived_from == "families"


def test_a_nurse_searches_for_nursing():
    joined = " ".join(profile_of(NURSE).titles)
    assert "nurse" in joined
    assert "devops" not in joined and "kubernetes" not in joined


def test_an_accountant_searches_for_accounting():
    joined = " ".join(profile_of(ACCOUNTANT).titles)
    assert "accountant" in joined
    assert "data engineer" not in joined


# -- one passing mention must not become the whole search -------------------

def test_a_single_body_mention_cannot_win():
    """The electrical resume says "networking" once, about protection relays.

    That one word used to be enough. `FAMILY_SCORE_FLOOR` is a FRACTION of the
    top family's score, so when the top family scored 1, the floor was 0.2 and
    the single mention cleared it — making an electrical engineer a network
    engineer. Not a fallback to the software defaults: a confident, specific,
    wrong answer, which is harder to notice and worse.
    """
    resume = parse_text(ELECTRICAL)
    families, _ = match_families(resume, ["Senior Electrical Engineer"])

    assert "network" not in families
    assert families == ["electrical_eng"]


def test_body_evidence_alone_needs_to_be_repeated():
    """The absolute gate, tested on its own: one mention is an aside, several
    is evidence. The relative floor then still ranks it against everything
    else, which is why a lone "networking" loses to a resume full of
    substations even after clearing this bar."""
    base = "JO B\nAustin, TX, USA | jo@example.com\n\nSUMMARY\n%s\n"

    once, _ = match_families(parse_text(base % "Some network engineer work."), [])
    assert once == []

    often, _ = match_families(
        parse_text(base % (" ".join(["network engineer."] * MIN_BODY_EVIDENCE))), [])
    assert "network" in often


# -- a held title is a fact, and is never discarded -------------------------

def test_held_titles_survive_a_family_match():
    """These used to be either/or: a matched family DISCARDED the held titles,
    so a near-miss match could drop "Electrical Engineer" from an electrical
    engineer's own search."""
    terms = build_title_keywords(["sre"], ["Electrical Engineer"])

    assert "electrical engineer" in terms
    assert "site reliability" in terms


# -- exclusions must not rule out the job the person actually does ----------

def test_staff_is_not_excluded_for_a_staff_nurse():
    """"Staff" and "manager" are seniority markers in a software career. In a
    hospital or a university they are the job: Staff Nurse, Staff Accountant,
    Laboratory Manager, Nurse Manager."""
    assert "staff" not in build_exclusions("mid", ["Staff Nurse"])
    assert "manager" not in build_exclusions("senior", ["Laboratory Manager"])
    # and the software case is unchanged
    assert "manager" in build_exclusions("senior", ["Senior Site Reliability Engineer"])


# -- seniority has to be readable from a resume that avoids the word --------

def test_years_are_read_without_the_word_experience():
    """"9 years in power systems" and "6 years as a registered nurse" both
    used to read as no experience at all — and a profile with no seniority and
    no family is exactly the one that fell through to the software defaults."""
    assert extract_years_experience(parse_text(ELECTRICAL)) == 9.0
    assert extract_years_experience(parse_text(NURSE)) == 6.0


def test_a_stray_number_is_still_not_experience():
    resume = parse_text("ALEX\nAustin, TX, USA\nAWS EC2 2019 certification\n")
    assert extract_years_experience(resume) is None
