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

The third time (Oct 20, 2026) it was wrong both ways in one list of 222. A
foreign name was checked before anything American, so "Albuquerque, New Mexico,
United States" was Mexico, "Dublin, OH" Ireland, "Vancouver, WA" Canada, and
"Remote (United States | Canada)" foreign; "PA, Remote", "TX-GRAND PRAIRIE",
"DC | Remote" and "San Mateo" named nothing it knew. Every fetch dropped such
postings. And "Remote" or "Hybrid" in front kept anything after it: "Hybrid
(Santiago, Las Condes)", "Remote (Time zone: CET)". Now each place in a string
is judged on its own, a sign only an American place gives counts first, a
country before a city, and after "Remote" a name is still a name.
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
    # Printed bare on boards in Oct 2026 and read as foreign until added.
    "san mateo", "chandler", "ventura", "dtla", "pennington",
}

# Places that are NOT the US, as boards write them; ml/resume/profile.py keeps a US-based search
# away from them. A country or region is firm evidence. A city is weaker: US places share the
# names ("Dublin, OH", "Vancouver, WA", "Paris, TX"), and boards list one beside a US city
# ("New York, London").
NON_US_REGIONS = (
    "emea", "apac", "latam", "anz", "europe", "european union", "eu remote",
    "asia", "africa", "middle east", "nordics", "benelux", "dach", "iberia",
)
NON_US_COUNTRIES = (
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
)
NON_US_CITIES = (
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai",
    "gurgaon", "noida", "london", "dublin", "berlin", "munich", "paris",
    "amsterdam", "barcelona", "madrid", "lisbon", "warsaw", "krakow", "prague",
    "bucharest", "sofia", "vilnius", "tallinn", "stockholm", "oslo",
    "copenhagen", "helsinki", "zurich", "tel aviv", "toronto", "vancouver",
    "montreal", "ottawa", "sydney", "melbourne", "auckland", "tokyo", "seoul",
    "shanghai", "beijing", "sao paulo", "mexico city", "buenos aires",
    "bogota", "lagos", "nairobi", "cairo", "dubai",
    # Metro Manila has a "National Capital Region" too, and alone it reads as Washington's:
    # "Taguig City, National Capital Region (Manila)" was kept as American (23 Oct 2026).
    "manila", "makati", "taguig", "cebu",
)

# Said instead of a place. Not evidence either way, and treating them as
# evidence was the original bug. A location made only of these says nowhere.
UNKNOWN = frozenset("""
    remote hybrid onsite on site in office offices flexible anywhere work from home wfh n a tbd
    unspecified multiple various several location locations country countries global globally
    worldwide world international any the of and or a an per day days week weeks first based
    fully full time part hours hour zone timezone tz negotiable after selection americas amer
    north america remoto remota pst pt pdt est et edt cst ct cdt mst mt mdt eastern pacific
    central mountain utc gmt no not clearance required needed optional only must be to with within
    near local metro area travel occasional candidates residents eligible preferred role position
    opportunity team ok
""".split())

# Regions with the US in them: "Remote - Americas, EMEA" is open here too, so a foreign name
# beside one does not settle it.
WITH_US = ("americas", "amer", "north america", "global", "globally", "worldwide", "anywhere")

# Time zones east of the Atlantic: "Remote (Time zone: CET (+/- 3 hours))" is a European job.
ABROAD_ZONES = frozenset("cet cest eet eest wet bst ist sgt jst kst hkt aest aedt awst nzst nzdt msk".split())

US_MARKER = re.compile(
    r"\b(?:u\.?s\.?a?\.?|usa|united states|stateside|conus)\b", re.I)

# Washington, D.C., as boards write it alone: "DC | Remote", "Local to D.C. metro area".
CAPITAL = re.compile(r"\bD\.\s?C\b|\bDC\b|\bnational capital region\b", re.I)

# "Boston, MA" / "Austin, TX 78701": a comma, then a state code in capitals, as boards write
# them ("Remote, in Bangalore" is not Indiana), the place kept (group 1).
CITY_STATE = re.compile(r"([^,|;()]*?)\s*,\s*([A-Z]{2})\b")
# A state code where only one goes: "PA, Remote", "TX-GRAND PRAIRIE", "AZ_Mesa_HQ" -- and before
# a ZIP code: "Whippany NJ 07981".
STATE_LEAD = re.compile(r"^\s*([A-Z]{2})\s*[,_-]")              # "In-Office" is not Indiana
STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
# A state code standing alone, in capitals: "Remote - MA", "Remote-CA", "Remote GA". Not OR or OK,
# which are words: "Remote OR Hybrid", "Remote OK".
STATE_ALONE = re.compile(r"(?<![A-Za-z])(?!OR\b|OK\b)([A-Z]{2})(?![A-Za-z])")
# State codes that are also countries' (ISO 3166). After a foreign place the country is meant:
# "Bengaluru, IN" is India, where "Dublin, OH" is Ohio.
COUNTRY_CODES = frozenset({"al", "ar", "az", "ca", "co", "de", "ga", "id", "il", "in", "ky", "la", "ma",
                           "md", "me", "mn", "mo", "ms", "mt", "nc", "ne", "pa", "sc", "sd", "tn", "va"})


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def _names(text: str, places) -> bool:
    padded = f" {_words(text)} "
    return any(f" {place} " in padded for place in places)


def _without_states(text: str) -> str:
    """The words with US state names taken out: "New Mexico" must not read as Mexico."""
    padded = f" {_words(text)} "
    for name in sorted(STATE_NAMES, key=len, reverse=True):
        padded = padded.replace(f" {name} ", " ")
    return padded


def _state_code(code: str, beside: str, abroad) -> bool:
    """A US state's code, unless it is also a country's and the place beside it is abroad."""
    code = code.lower()
    return code in STATE_CODES and not (code in COUNTRY_CODES and _names(beside, abroad))


def _place(raw: str, non_us_places) -> bool | None:
    """One place: True in the US, False abroad, None when it names nowhere."""
    abroad = {p.strip().lower() for p in non_us_places if p and p.strip()}
    cities = abroad & set(NON_US_CITIES)
    padded = f" {_words(raw)} "
    # 1. What only an American place says.
    named_abroad = _names(raw, abroad | set(NON_US_CITIES))
    if US_MARKER.search(raw) or (CAPITAL.search(raw) and not named_abroad):    # not "Bogotá, D.C."
        return True
    if any(_state_code(code, place, abroad | set(NON_US_CITIES)) for place, code in CITY_STATE.findall(raw)):
        return True
    lead = STATE_ZIP.search(raw) or STATE_LEAD.match(raw)
    if lead and _state_code(lead.group(1), raw, abroad | set(NON_US_CITIES)):
        return True
    if any(_state_code(code, raw, abroad | set(NON_US_CITIES)) for code in STATE_ALONE.findall(raw)):
        return True
    # 2. A country or region abroad -- unless a region with the US in it is named as well.
    if any(f" {place} " in _without_states(raw) for place in abroad - cities):
        return None if any(f" {word} " in padded for word in WITH_US) else False
    # 3. A state, or a US city.
    if any(f" {name} " in padded for name in STATE_NAMES) or any(f" {city} " in padded for city in US_CITIES):
        return True
    # 4. A city abroad.
    if any(f" {place} " in padded for place in cities):
        return False
    # 5. Nowhere named: silence, and silence is kept.
    words = padded.split()
    if all(word in UNKNOWN or word.isdigit() for word in words):
        return None
    if any(word in ABROAD_ZONES for word in words):
        return False
    # 6. Somewhere named, and nothing about it American -- "Remote" in front changes nothing:
    # "Hybrid (Santiago, Las Condes)". Unless the rest is not a name at all ("3 days a week").
    # A word in capitals is a company's or a program's name more often than a place's:
    # "Any NISC Location".
    rest = [w for w in re.findall(r"[A-Za-z][\w'.-]*", raw)
            if w.lower() not in UNKNOWN and not (len(w) > 2 and w.isupper())]
    if not rest or not any(w[0].isupper() for w in rest):
        return None
    return False


def is_us(location: str | None, non_us_places=()) -> bool | None:
    """True if in the US, False if definitely not, None if it cannot be told.

    `non_us_places` is the caller's list of excluded regions, countries and cities. Several
    places in one string ("Chicago; London", "Remote Canada | Remote United States") are
    judged one by one: a posting open in a US place is a US posting.
    """
    raw = (location or "").strip()
    if not raw:
        return None
    verdicts = [_place(part, non_us_places) for part in re.split(r"[|;]", raw) if part.strip()]
    if True in verdicts:
        return True
    if False in verdicts:
        return False
    return None


def reject_reason(location: str | None, non_us_places=()) -> str | None:
    """Why this location is not acceptable, or None if it is."""
    verdict = is_us(location, non_us_places)
    if verdict is False:
        return f"location {location!r} is not in the US"
    return None
