"""Derive a job search from a resume — no keyword list to hand-write.

The old flow asked the user to fill in `filters.title_keywords` themselves.
Nobody knows the right answer to that on day one, so the default list stayed in
place and the tool searched for the wrong jobs: an 11-year SRE resume against a
generic "software engineer / backend / full stack" search that also excluded
"staff" and "principal" — the two levels that person should most be looking at.

So the resume writes the search instead. Drop in any resume, get a search.

Two layers, so it always works:

  * OFFLINE (this module) — reads titles, skills, and years of experience out
    of the resume text. No API key, no cost, fully deterministic and testable.
  * REFINED (later, needs a key) — Claude reads the resume for careers the
    offline layer's vocabulary does not cover. `refine_with_claude` is the
    hook; the offline result is always the starting point, never skipped.

The derived search is PRINTED and SAVED to a file the user can edit. "No input
required" must not mean "no way to see what it decided" — a silent wrong guess
scrapes the wrong jobs for weeks, and that is worse than asking.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from resume.parser import Resume

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Role vocabulary.
#
# Each family maps the phrases that IDENTIFY it (in a resume) to the search
# terms it should PRODUCE (for a job board). The two are different: a resume
# says "Site Reliability Engineer", boards also post the same job as "SRE",
# "Infrastructure Engineer", and "Production Engineer".
#
# This vocabulary is inevitably strongest where it has been used. A resume that
# matches no family falls back to the titles the person actually held, which is
# never wrong — just narrower. That fallback is what keeps a nurse or an
# accountant from getting a software engineer's search.
# ---------------------------------------------------------------------------
ROLE_FAMILIES: dict[str, dict[str, list[str]]] = {
    "sre": {
        "identifies": ["site reliability", "sre", "production engineer"],
        "searches": ["site reliability", "sre", "production engineer", "reliability engineer"],
    },
    "devops": {
        "identifies": ["devops", "dev ops", "ci/cd", "release engineer"],
        "searches": ["devops", "dev ops", "release engineer", "build engineer"],
    },
    "cloud": {
        "identifies": ["cloud engineer", "cloud architect", "cloud infrastructure", "aws", "azure"],
        "searches": ["cloud engineer", "cloud architect", "cloud infrastructure", "cloud platform"],
    },
    "platform": {
        "identifies": ["platform engineer", "kubernetes", "terraform", "infrastructure as code"],
        "searches": ["platform engineer", "infrastructure engineer", "systems engineer"],
    },
    "security": {
        "identifies": ["devsecops", "security engineer", "appsec", "vulnerability"],
        "searches": ["devsecops", "security engineer", "cloud security", "infrastructure security"],
    },
    "backend": {
        "identifies": ["backend", "back end", "api development", "microservices"],
        "searches": ["backend engineer", "back end engineer", "software engineer"],
    },
    "fullstack": {
        "identifies": ["full stack", "fullstack"],
        "searches": ["full stack", "fullstack", "software engineer"],
    },
    "frontend": {
        "identifies": ["frontend", "front end", "react developer"],
        "searches": ["frontend engineer", "front end engineer", "ui engineer"],
    },
    "data_eng": {
        "identifies": ["data engineer", "etl", "data pipeline", "airflow"],
        "searches": ["data engineer", "analytics engineer", "data platform"],
    },
    "ml": {
        "identifies": ["machine learning", "ml engineer", "deep learning"],
        "searches": ["machine learning engineer", "ml engineer", "mlops"],
    },
    "data_sci": {
        "identifies": ["data scientist", "data science"],
        "searches": ["data scientist", "data science"],
    },
    "qa": {
        "identifies": ["qa engineer", "test engineer", "automation testing", "sdet"],
        "searches": ["qa engineer", "test engineer", "sdet", "automation engineer"],
    },
    "mobile": {
        "identifies": ["ios developer", "android developer", "mobile developer"],
        "searches": ["mobile engineer", "ios engineer", "android engineer"],
    },
    "network": {
        "identifies": ["network engineer", "networking", "cisco"],
        "searches": ["network engineer", "network operations"],
    },
    "sysadmin": {
        "identifies": ["system administrator", "sysadmin", "linux administrator"],
        "searches": ["systems administrator", "linux engineer", "systems engineer"],
    },
    "product": {
        "identifies": ["product manager", "product owner"],
        "searches": ["product manager", "product owner"],
    },
    "design": {
        "identifies": ["ux designer", "ui designer", "product designer", "figma"],
        "searches": ["product designer", "ux designer", "ui designer"],
    },
}

# Seniority words, and the level they signal.
SENIORITY_WORDS = {
    "intern": 0, "internship": 0, "graduate": 0, "new grad": 0, "entry level": 0,
    "junior": 1, "associate": 1,
    "mid level": 2,
    "senior": 3, "sr.": 3, "sr ": 3,
    "lead": 4, "staff": 4,
    "principal": 5, "distinguished": 5, "architect": 4,
}

# Titles that mean managing people, not doing the work.
MANAGEMENT_WORDS = ["manager", "director", "vp", "vice president", "head of", "chief"]

# Never useful to anyone in this tool's scope.
ALWAYS_EXCLUDE = ["recruiter", "sales", "account executive", "intern", "internship"]


@dataclass
class SearchProfile:
    """What to search for, derived from one resume."""

    titles: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude_locations: list[str] = field(default_factory=list)
    years_experience: float | None = None
    seniority: str = "unknown"
    families: list[str] = field(default_factory=list)
    considered_families: list[str] = field(default_factory=list)
    held_titles: list[str] = field(default_factory=list)
    source: str = "resume (offline)"
    resume_path: str | None = None

    def describe(self) -> str:
        years = f"{self.years_experience:.0f}+ years" if self.years_experience else "unknown"
        return (
            f"{self.seniority} · {years} · "
            f"{', '.join(self.families) if self.families else 'no family matched'}"
        )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_years_experience(resume: Resume, now: datetime | None = None) -> float | None:
    """Years of experience, from an explicit claim or the earliest job date.

    An explicit "11+ years of experience" wins: it is the person's own summary
    of their career, and it survives resume layouts this parser cannot segment.
    """
    text = resume.text()

    claim = re.search(r"(\d{1,2})\s*\+?\s*years?(?:\s+of)?\s+(?:of\s+)?experience", text, re.I)
    if claim:
        return float(claim.group(1))

    # Fall back to the span from the earliest four-digit year that appears next
    # to a month — a date range, not a random number like "AWS EC2 2019".
    now = now or datetime.now()
    years = [
        int(m)
        for m in re.findall(
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})", text, re.I
        )
    ]
    if not years:
        return None
    earliest = min(years)
    if not (1960 < earliest <= now.year):
        return None
    return float(now.year - earliest)


def extract_held_titles(resume: Resume) -> list[str]:
    """Job titles this person has actually held.

    Three places a title hides: the headline under the name, the section
    headings a resume uses for each role, and the line above a date range.
    """
    titles: list[str] = []

    header = resume.section("HEADER")
    if header:
        for line in header.lines:
            # "Site reliability Engineer | SRE, DevOps & Platform Engineering | AWS"
            for part in re.split(r"[|•·,/]", line):
                part = part.strip()
                if 3 <= len(part) <= 60 and _looks_like_title(part):
                    titles.append(part)

    for section in resume.sections:
        if _looks_like_title(section.heading) and section.heading.upper() not in (
            "HEADER", "SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION"
        ):
            titles.append(section.heading.strip())

    for section in resume.sections:
        previous = None
        for line in section.lines:
            if re.search(r"\d{4}\s*[–—-]\s*(present|\d{4})", line, re.I) and previous:
                if _looks_like_title(previous):
                    titles.append(previous.strip())
            previous = line

    # "as a Cloud Infrastructure Architect / Senior DevOps Engineer"
    for match in re.finditer(r"experience as an?\s+([A-Za-z /&-]{5,70})", resume.text(), re.I):
        for part in match.group(1).split("/"):
            if _looks_like_title(part):
                titles.append(part.strip())

    return _dedupe([t for t in titles if t])


TITLE_WORDS = (
    "engineer", "developer", "architect", "administrator", "analyst", "scientist",
    "designer", "manager", "consultant", "specialist", "lead", "director",
    "nurse", "accountant", "teacher", "technician", "researcher", "writer",
)


def _looks_like_title(text: str) -> bool:
    lowered = text.lower().strip()
    if not 3 <= len(lowered) <= 70:
        return False
    return any(word in lowered for word in TITLE_WORDS)


# A phrase in a JOB TITLE is far stronger evidence than the same phrase in the
# body. "Site reliability Engineer" appears twice in this resume's titles and
# three times in its text; "networking" appears six times in the body and never
# as a title. Unweighted counting therefore ranks network engineering above
# SRE, which is exactly backwards.
TITLE_WEIGHT = 10

# Keep a family only if it has at least this share of the best family's score,
# and keep at most this many. Without a floor, one passing mention of "data
# pipeline" adds "data engineer" to the search forever.
FAMILY_SCORE_FLOOR = 0.20
MAX_FAMILIES = 5


def score_families(resume: Resume, held_titles: list[str]) -> list[tuple[str, int]]:
    """Every family with any evidence, best first, as (family, score)."""
    body = resume.text().lower()
    titles = " ".join(held_titles).lower()

    scored: list[tuple[str, int]] = []
    for family, spec in ROLE_FAMILIES.items():
        score = sum(
            body.count(phrase) + TITLE_WEIGHT * titles.count(phrase)
            for phrase in spec["identifies"]
        )
        if score:
            scored.append((family, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def match_families(resume: Resume, held_titles: list[str]) -> tuple[list[str], list[str]]:
    """Split families into (strong enough to search, considered but dropped)."""
    scored = score_families(resume, held_titles)
    if not scored:
        return [], []

    floor = scored[0][1] * FAMILY_SCORE_FLOOR
    strong = [family for family, score in scored if score >= floor][:MAX_FAMILIES]
    dropped = [family for family, _ in scored if family not in strong]
    return strong, dropped


def seniority_for(years: float | None, held_titles: list[str]) -> str:
    """Band this person's level, preferring what their titles already say."""
    title_text = " ".join(held_titles).lower()
    highest = max(
        (level for word, level in SENIORITY_WORDS.items() if word in title_text),
        default=None,
    )

    if years is None:
        band = {0: "entry", 1: "junior", 2: "mid", 3: "senior", 4: "lead", 5: "principal"}
        return band.get(highest, "unknown") if highest is not None else "unknown"

    if years < 2:
        derived = "entry"
    elif years < 4:
        derived = "junior"
    elif years < 7:
        derived = "mid"
    elif years < 11:
        derived = "senior"
    else:
        derived = "lead"

    # A title beats the arithmetic in one direction only: someone already a
    # "Senior X" is not junior no matter how the dates parse.
    if highest is not None and highest >= 3 and derived in ("entry", "junior", "mid"):
        return "senior"
    return derived


def build_title_keywords(families: list[str], held_titles: list[str]) -> list[str]:
    """Search terms to look for on a board."""
    terms: list[str] = []
    for family in families:
        terms.extend(ROLE_FAMILIES[family]["searches"])

    if not terms:
        # No family matched — search for what this person actually was. Narrow,
        # but never wrong, and it is what makes a non-tech resume work at all.
        terms = [t.lower() for t in held_titles]

    return _dedupe([t.lower() for t in terms])


def build_exclusions(seniority: str, held_titles: list[str]) -> list[str]:
    """Levels and role types to keep out of the results."""
    excludes = list(ALWAYS_EXCLUDE)

    below = {
        "entry": [],
        "junior": ["principal", "staff", "director"],
        "mid": ["junior", "principal", "director"],
        "senior": ["junior", "graduate", "entry level", "associate"],
        "lead": ["junior", "graduate", "entry level", "associate"],
        "principal": ["junior", "graduate", "entry level", "associate", "mid level"],
        "unknown": [],
    }
    excludes.extend(below.get(seniority, []))

    if seniority in ("entry", "junior"):
        excludes.extend(["senior", "lead", "staff"])

    # Only exclude people-management roles if this person has never been one.
    title_text = " ".join(held_titles).lower()
    if not any(word in title_text for word in MANAGEMENT_WORDS):
        excludes.extend(["manager", "director", "vp ", "head of"])

    return _dedupe(excludes)


# Places that are NOT the US, as they actually appear in job-board location
# strings. The previous blocklist had 13 entries and let through "Sweden
# (Remote)", "Spain (Remote)", "The Netherlands | Remote", "Remote - European
# Union" and "Remote - UK" — each matched the "remote" include term and named
# no blocked country.
#
# A blocklist of guesses can never be complete. This one is instead broad
# enough to cover where these boards actually hire, and it is consulted only
# when the person's own contact details are US-based.
NON_US_PLACES = [
    # regions and shorthands
    "emea", "apac", "latam", "anz", "europe", "european union", "eu remote",
    "asia", "africa", "middle east", "nordics", "benelux", "dach", "iberia",
    # countries
    "argentina", "armenia", "australia", "austria", "bangladesh", "belarus",
    "belgium", "bolivia", "brazil", "bulgaria", "cambodia", "canada", "chile",
    "china", "colombia", "costa rica", "croatia", "cyprus", "czech republic",
    "czechia", "denmark", "ecuador", "egypt", "estonia", "finland", "france",
    "germany", "ghana", "greece", "guatemala", "hong kong", "hungary",
    "iceland", "india", "indonesia", "ireland", "israel", "italy", "japan",
    "jordan", "kenya", "latvia", "lebanon", "lithuania", "luxembourg",
    "malaysia", "malta", "mexico", "morocco", "netherlands", "new zealand",
    "nigeria", "norway", "pakistan", "panama", "paraguay", "peru",
    "philippines", "poland", "portugal", "romania", "russia", "saudi arabia",
    "serbia", "singapore", "slovakia", "slovenia", "south africa",
    "south korea", "korea", "spain", "sri lanka", "sweden", "switzerland",
    "taiwan", "thailand", "tunisia", "turkey", "ukraine",
    "united arab emirates", "uae", "united kingdom", "uk", "uruguay",
    "venezuela", "vietnam",
    # cities that appear on job boards without their country
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai",
    "gurgaon", "noida", "london", "dublin", "berlin", "munich", "paris",
    "amsterdam", "barcelona", "madrid", "lisbon", "warsaw", "krakow", "prague",
    "bucharest", "sofia", "vilnius", "tallinn", "stockholm", "oslo",
    "copenhagen", "helsinki", "zurich", "tel aviv", "toronto", "vancouver",
    "montreal", "ottawa", "sydney", "melbourne", "auckland", "tokyo", "seoul",
    "shanghai", "beijing", "sao paulo", "mexico city", "buenos aires",
    "bogota", "lagos", "nairobi", "cairo", "dubai",
]


def extract_locations(resume: Resume) -> tuple[list[str], list[str]]:
    """Where this person can work, from the contact line.

    Returns (include, exclude). Exclusions win over inclusions in the filter,
    which is what separates "Remote - USA" from "Remote - India" when both
    match the include term "remote".
    """
    header = resume.section("HEADER")
    text = (header.text() if header else resume.text()[:400]).lower()

    include: list[str] = ["remote"]
    us_based = bool(
        re.search(r"\b(usa|united states|u\.s\.)\b", text)
        or re.search(
            r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|"
            r"mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)\b,?\s*usa?\b",
            text,
        )
    )
    if us_based:
        include.extend(["united states", "usa", "us"])
        # Never exclude somewhere this person actually is. Only the contact
        # line is consulted, so a degree or past role abroad does not count.
        return _dedupe(include), [p for p in NON_US_PLACES if p not in text]

    # Somewhere else, or undeterminable. Excluding every non-US place would be
    # wrong, and inferring a country from a phone number is worse. Keep every
    # location and let the person narrow it in the profile file.
    return _dedupe(include), []


def derive_search_profile(resume: Resume, now: datetime | None = None) -> SearchProfile:
    """Turn a parsed resume into a job search. Offline, deterministic."""
    held = extract_held_titles(resume)
    years = extract_years_experience(resume, now=now)
    families, considered = match_families(resume, held)
    seniority = seniority_for(years, held)
    include_loc, exclude_loc = extract_locations(resume)

    profile = SearchProfile(
        titles=build_title_keywords(families, held),
        exclude_titles=build_exclusions(seniority, held),
        locations=include_loc,
        exclude_locations=exclude_loc,
        years_experience=years,
        seniority=seniority,
        families=families,
        considered_families=considered,
        held_titles=held,
        resume_path=str(resume.source_path) if resume.source_path else None,
    )

    # An exclusion that kills the search is a bug, not a filter: "senior" as an
    # exclusion while every search term is a senior role returns nothing.
    profile.exclude_titles = [
        term for term in profile.exclude_titles
        if not any(term in title for title in profile.titles)
    ]
    return profile


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


# ---------------------------------------------------------------------------
# Persistence.
#
# The derived search is written to a real file for two reasons: the user can
# see what was decided on their behalf, and they can correct it. "Requires no
# input" is about the first run, not about being unable to disagree.
# ---------------------------------------------------------------------------

PROFILE_FILENAME = "config/search_profile.yaml"


def save_profile(profile: SearchProfile, path) -> None:
    """Write the derived search out as readable, editable YAML."""
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def block(name: str, values: list[str]) -> str:
        if not values:
            return f"{name}: []\n"
        lines = "\n".join(f'  - "{v}"' for v in values)
        return f"{name}:\n{lines}\n"

    considered = ", ".join(profile.considered_families) or "none"
    years = f"{profile.years_experience:.0f}" if profile.years_experience else "unknown"

    path.write_text(
        "# Job search derived from your resume — generated, not hand-written.\n"
        "#\n"
        f"# Resume    : {profile.resume_path or 'unknown'}\n"
        f"# Generated : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"# Read as   : {profile.seniority} level, {years} years, "
        f"{', '.join(profile.families) or 'no role family matched'}\n"
        f"# Also considered but not strong enough in your resume: {considered}\n"
        "#\n"
        "# EDIT THIS FILE FREELY. The scraper reads it in preference to the\n"
        "# filters in config.yaml. Regenerate from the resume with:\n"
        "#     python main.py profile --force      (overwrites your edits)\n"
        "\n"
        f"seniority: {profile.seniority}\n"
        f"years_experience: {years}\n"
        + block("families", profile.families)
        + "\n# Job titles to search for.\n"
        + block("title_keywords", profile.titles)
        + "\n# Dropped even if the title matched above.\n"
        + block("exclude_title_keywords", profile.exclude_titles)
        + "\n# Where you can work.\n"
        + block("location_keywords", profile.locations)
        + block("exclude_location_keywords", profile.exclude_locations),
        encoding="utf-8",
    )


def load_profile(path) -> SearchProfile | None:
    """Read a saved search profile, or None if there isn't one."""
    from pathlib import Path

    import yaml

    path = Path(path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    years = data.get("years_experience")
    return SearchProfile(
        titles=list(data.get("title_keywords") or []),
        exclude_titles=list(data.get("exclude_title_keywords") or []),
        locations=list(data.get("location_keywords") or []),
        exclude_locations=list(data.get("exclude_location_keywords") or []),
        years_experience=float(years) if isinstance(years, (int, float)) else None,
        seniority=str(data.get("seniority", "unknown")),
        families=list(data.get("families") or []),
        source=f"saved profile ({path.name})",
    )


def refine_with_claude(profile: SearchProfile, resume: Resume, cfg) -> SearchProfile:
    """Hook for the API-backed layer. Not built yet — returns the offline result.

    The offline vocabulary above is strongest on technical resumes. A nurse, an
    accountant, or a paralegal falls through to their own held titles, which is
    narrow but honest. Claude reading the resume directly is what makes the
    derivation genuinely career-agnostic, and it needs an API key the project
    does not have yet.
    """
    return profile
