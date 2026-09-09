"""Search-profile derivation tests — offline, no resume file, no network.

The point of these is that the derivation runs with NO user input, so a wrong
guess is silent. Most of what is tested here is therefore the guessing: that a
passing mention does not become a search term, that seniority is not read off
arithmetic alone, and that a non-technical resume does not get handed a
software engineer's search.
"""

import pytest

from resume.parser import parse_text
from resume.profile import (
    build_exclusions,
    derive_search_profile,
    extract_years_experience,
    load_profile,
    save_profile,
    score_families,
    seniority_for,
)

SRE_RESUME = """Shyam Sunder Daggupati
GA, USA (Open to Relocate) | +1 (555) 555-5555 | someone@example.com
Site reliability Engineer | SRE, DevOps & Platform Engineering | AWS | Terraform
SUMMARY
11+ years of experience as a Cloud Infrastructure Architect / Senior DevOps Engineer.
Strong expertise in Infrastructure as Code using Terraform across environments.
Cloud networking fundamentals such as VPC/VNet design, subnets, routing tables.
EXPERIENCE
SENIOR Site reliability Engineer
Top Vocation, USA  Feb 2025 - Present
Architected and operated Kubernetes platforms (EKS, AKS) on VM infrastructure.
Built CI/CD pipelines using GitHub Actions, Azure DevOps, and Jenkins.
SKILLS
- Cloud Platforms: AWS (EC2, S3, EKS), Azure (AKS, Key Vault)
- CI/CD & GitOps: Jenkins, Azure DevOps, ArgoCD, Helm
EDUCATION
Master of Science  Sep 2023 - Dec 2024
"""

JUNIOR_FRONTEND = """Alex Kim
Boston, MA, USA | alex@example.com
Frontend Developer
SUMMARY
2 years of experience building React interfaces.
EXPERIENCE
Junior Frontend Developer
Widgets Inc  Jan 2024 - Present
Built React components and CSS design systems.
SKILLS
- React, TypeScript, CSS
"""

NURSE_RESUME = """Maria Santos
Austin, TX, USA | maria@example.com
Registered Nurse
SUMMARY
8 years of experience in acute care nursing.
EXPERIENCE
Registered Nurse
County Hospital  Mar 2019 - Present
Delivered patient care in a 30-bed medical-surgical unit.
Charge Nurse
City Clinic  Jun 2017 - Feb 2019
SKILLS
- Patient assessment, IV therapy, EMR (Epic)
EDUCATION
Bachelor of Science in Nursing
"""


def profile_of(text):
    return derive_search_profile(parse_text(text))


# -- reading the resume ----------------------------------------------------

def test_explicit_years_claim_wins():
    assert extract_years_experience(parse_text(SRE_RESUME)) == 11.0


def test_years_returns_none_when_undeterminable():
    assert extract_years_experience(parse_text("Jane Doe\nSKILLS\nPython")) is None


def test_held_titles_are_extracted():
    held = " ".join(profile_of(SRE_RESUME).held_titles).lower()
    assert "site reliability" in held
    assert "cloud infrastructure architect" in held


# -- the guessing, which is where silent errors live -----------------------

def test_title_evidence_outweighs_body_mentions():
    """"networking" appears in the body; "Site reliability Engineer" is the
    actual title. Unweighted counting ranked network engineering above SRE."""
    profile = profile_of(SRE_RESUME)
    assert "sre" in profile.families
    assert "network" not in profile.families


def test_weak_families_are_reported_not_silently_dropped():
    profile = profile_of(SRE_RESUME)
    dropped = set(profile.considered_families)
    assert dropped and not (dropped & set(profile.families))


def test_families_are_ranked_by_evidence():
    scores = dict(score_families(parse_text(SRE_RESUME), []))
    # Cloud is all over this resume; networking is one passing mention.
    assert scores["cloud"] > scores["network"]
    assert "frontend" not in scores  # no evidence at all -> not scored


# -- seniority -------------------------------------------------------------

def test_long_career_reads_as_lead():
    assert profile_of(SRE_RESUME).seniority == "lead"


def test_short_career_reads_as_junior():
    assert profile_of(JUNIOR_FRONTEND).seniority == "junior"


def test_senior_title_beats_low_year_count():
    """Someone already a "Senior X" is not junior however the dates parse."""
    assert seniority_for(3.0, ["Senior Platform Engineer"]) == "senior"


def test_seniority_unknown_without_years_or_title():
    assert seniority_for(None, ["Engineer"]) == "unknown"


# -- what gets excluded ----------------------------------------------------

def test_experienced_person_is_not_shown_junior_roles():
    excludes = profile_of(SRE_RESUME).exclude_titles
    assert "junior" in excludes
    assert "entry level" in excludes


def test_experienced_person_is_not_excluded_from_staff_or_principal():
    """The old hand-written default excluded both — the two levels an
    11-year engineer should most be seeing."""
    excludes = profile_of(SRE_RESUME).exclude_titles
    assert "staff" not in excludes
    assert "principal" not in excludes


def test_junior_is_not_shown_senior_roles():
    excludes = profile_of(JUNIOR_FRONTEND).exclude_titles
    assert "senior" in excludes and "staff" in excludes


def test_management_excluded_for_a_non_manager():
    assert "manager" in build_exclusions("senior", ["Senior DevOps Engineer"])


def test_management_kept_for_someone_who_has_managed():
    assert "manager" not in build_exclusions("lead", ["Engineering Manager"])


def test_an_exclusion_never_cancels_a_search_term():
    """A "senior" exclusion alongside "senior sre" as a search term returns
    nothing at all — a self-defeating filter, not a strict one."""
    profile = profile_of(SRE_RESUME)
    for excluded in profile.exclude_titles:
        assert not any(excluded in title for title in profile.titles)


# -- careers the offline vocabulary does not cover --------------------------

def test_non_technical_resume_falls_back_to_its_own_titles():
    """A nurse must not be handed a software engineer's search. Falling back
    to held titles is narrow but honest — and it is why this is safe to run
    on any resume before the Claude layer exists."""
    profile = profile_of(NURSE_RESUME)
    joined = " ".join(profile.titles).lower()
    assert "nurse" in joined
    assert "software engineer" not in joined
    assert "devops" not in joined


# -- locations -------------------------------------------------------------

def test_us_resume_searches_us_and_remote():
    locations = profile_of(SRE_RESUME).locations
    assert "remote" in locations and "united states" in locations


def test_home_country_is_not_excluded():
    profile = profile_of(NURSE_RESUME)
    assert "usa" not in [c.lower() for c in profile.exclude_locations]


# -- the file the user can edit --------------------------------------------

def test_saved_profile_round_trips(tmp_path):
    original = profile_of(SRE_RESUME)
    path = tmp_path / "search_profile.yaml"
    save_profile(original, path)

    loaded = load_profile(path)
    assert loaded.titles == original.titles
    assert loaded.exclude_titles == original.exclude_titles
    assert loaded.seniority == original.seniority


def test_saved_profile_explains_itself(tmp_path):
    """The file has to say what was decided and how to change it — deriving
    without input is only safe if the result is visible."""
    path = tmp_path / "search_profile.yaml"
    save_profile(profile_of(SRE_RESUME), path)
    text = path.read_text()
    assert "EDIT THIS FILE FREELY" in text
    assert "--force" in text


def test_load_profile_missing_file_returns_none(tmp_path):
    assert load_profile(tmp_path / "nope.yaml") is None
