"""Deciding whether a posting's location is in the United States.

This has been wrong twice, in opposite directions, and both times the failure
was invisible until someone looked at a specific job.

First it required a location to contain "remote", "united states", "usa" or
"us". "Boston, MA" contains none of those, so every onsite posting that named
only a city and state was silently discarded -- 0 of 3,590 university campuses
survived it. It went unnoticed because Greenhouse, Lever and Ashby all append
", United States", so on those boards the rule rejected nothing at all.

Then it required nothing: an empty include list, relying on a list of excluded
countries. That list is country-level, so "Kfar Saba" -- a city in Israel --
walked through and ranked first at 94%. "Getafe Area" and "Rome Metropolitain
Area" too. Israel was on the exclusion list; the posting simply never said
Israel.

Neither a blocklist of foreign places nor a blanket allow works, because job
boards write locations however they like. What does work is asking three
questions in order and, crucially, having an honest third answer:

    is_us("Boston, MA")        -> True      names a state
    is_us("Kfar Saba")         -> False     a place, with nothing US about it
    is_us("Remote")            -> None      cannot tell, and pretending is worse

`None` is the important one. A posting that says only "Remote" or "Hybrid" is
not evidence of anything, and dropping those was the first bug. They are kept.
"""

from __future__ import annotations

import re

STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr", "vi", "gu",
}

STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}

# US cities that boards routinely print with no state after them. Without
# these, 74 real US postings in one snapshot -- San Francisco, Chicago,
# Austin, Boston, Los Angeles, Plano -- would be read as foreign.
US_CITIES = {
    "new york", "new york city", "nyc", "brooklyn", "manhattan", "queens",
    "los angeles", "san francisco", "sf", "san jose", "palo alto", "mountain view",
    "sunnyvale", "santa clara", "cupertino", "menlo park", "redwood city",
    "oakland", "berkeley", "san diego", "sacramento", "irvine", "pasadena",
    "santa monica", "long beach", "fremont", "chicago", "boston", "cambridge",
    "somerville", "waltham", "austin", "dallas", "houston", "san antonio",
    "plano", "fort worth", "seattle", "bellevue", "redmond", "portland",
    "denver", "boulder", "atlanta", "miami", "orlando", "tampa", "phoenix",
    "scottsdale", "tempe", "las vegas", "salt lake city", "minneapolis",
    "st paul", "detroit", "ann arbor", "columbus", "cleveland", "cincinnati",
    "pittsburgh", "philadelphia", "baltimore", "washington dc", "arlington",
    "alexandria", "reston", "mclean", "bethesda", "raleigh", "durham",
    "charlotte", "nashville", "memphis", "new orleans", "kansas city",
    "st louis", "saint louis", "indianapolis", "milwaukee", "madison",
    "des moines", "omaha", "oklahoma city", "tulsa", "albuquerque", "boise",
    "spokane", "tucson", "richmond", "virginia beach", "jacksonville",
    "louisville", "buffalo", "rochester", "albany", "hartford", "providence",
    "stamford", "newark", "jersey city", "princeton", "trenton",
}

# Said instead of a place. Not evidence either way, and treating them as
# evidence was the original bug.
VAGUE = re.compile(
    r"^\s*(?:remote|hybrid|on-?site|in[- ]office|flexible|anywhere|multiple"
    r"|various|several|n/?a|tbd|unspecified|work from home|wfh)\b",
    re.I,
)

US_MARKER = re.compile(
    r"\b(?:u\.?s\.?a?\.?|usa|united states|stateside|conus)\b", re.I)

# "Boston, MA" / "Austin, TX 78701" — a comma, then a state code.
CITY_STATE = re.compile(r",\s*([A-Za-z]{2})\b")


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def is_us(location: str | None, non_us_places=()) -> bool | None:
    """True if in the US, False if definitely not, None if it cannot be told.

    `non_us_places` is the caller's list of excluded countries and cities; it
    is checked first, because an explicit "Remote - India" should be rejected
    on the strength of the word India rather than on the absence of a state.
    """
    raw = (location or "").strip()
    if not raw:
        return None

    text = _words(raw)
    padded = f" {text} "

    for place in non_us_places:
        place = (place or "").strip().lower()
        if place and f" {place} " in padded:
            return False

    # A US signal anywhere in the string settles it, even alongside a vague
    # word: "Remote - US" and "US Remote" are both American.
    if US_MARKER.search(raw):
        return True
    if any(f" {name} " in padded for name in STATE_NAMES):
        return True
    found = CITY_STATE.search(raw)
    if found and found.group(1).lower() in STATE_CODES:
        return True
    if any(f" {city} " in padded for city in US_CITIES):
        return True

    # No US signal. If the string never named a place at all, that is not
    # evidence of being abroad -- it is silence, and silence is kept.
    if VAGUE.match(raw):
        return None

    # It named somewhere, and nothing about it is American.
    return False


def reject_reason(location: str | None, non_us_places=()) -> str | None:
    """Why this location is not acceptable, or None if it is."""
    verdict = is_us(location, non_us_places)
    if verdict is False:
        return f"location {location!r} is not in the US"
    return None
