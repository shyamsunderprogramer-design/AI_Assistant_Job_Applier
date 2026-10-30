"""Facts a posting states about itself: workplace, experience, pay.

All three are already in text the scrape stores, so nothing here touches the
network. What each function refuses to answer matters as much as what it
returns — a confident wrong salary is worse than a blank one, because a blank
prompts you to go and look.

None means "the posting did not say". Callers render that; it is never stored
as words, or every filter would have to parse English back out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# HTML becomes text carrying &nbsp; as U+00A0, and a pattern written with an
# ordinary space silently stops matching. One posting's "With\xa0nearly 20
# years" slipped every company-history guard because of exactly this.
_UNICODE_SPACE = re.compile(r"[\u00a0\u2007\u202f\u2009\u200a\u2002-\u2006]")


_BLANKS = re.compile(r"[^\S\n]+")         # spaces and tabs, not line breaks
_LINES = re.compile(r"\s*\n\s*")

# Where a job site's own panels start under a posting copied with its whole page.
_PAGE_END = re.compile(r"^[^\S\n]*(stats for this job|salary comparison|receive similar jobs|similar jobs|"
                       r"popular searches|jobs you may (?:also )?like|people also viewed|more jobs like this|"
                       r"report this job|show more jobs)\b", re.I | re.M)


def posting_only(text: str | None, title: str = "") -> str:
    """A posting pasted with its whole page, cut to the posting: from the job's title (when the page
    names it near the top) to where the site's own panels start.

    On 22 Oct 2026 an Adzuna page pasted whole put its menus, salary charts and "Similar jobs" --
    other employers, other cities -- into a posting, and "neural concept" and "san francisco" were
    offered as kinds of work to claim. A cut that would leave under 50 words is not made.
    """
    text = text or ""
    start = 0
    found = re.search(re.escape(title.strip()), text, re.I) if title and title.strip() else None
    if found and found.start() < len(text) * 0.3:
        start = found.start()
    end = _PAGE_END.search(text, start + 200)
    body = text[start:end.start() if end else len(text)].rstrip()
    body = re.sub(r"\n[^\S\n]*apply for this job[^\S\n]*$", "", body, flags=re.I).rstrip()
    return body if len(body.split()) >= 50 else text


def _normalise(text: str | None) -> str:
    """Ordinary spaces, so a pattern written with one keeps working -- and a run of blanks as one
    space, a run of blank lines as one line break. With long runs left in, patterns that allow
    blanks in more than one place (between a number, a dash and another number) tried every way of
    sharing them out: a posting with 20,000 blanks after a number took 16 seconds, longer runs minutes."""
    if not text:
        return ""
    return _LINES.sub("\n", _BLANKS.sub(" ", _UNICODE_SPACE.sub(" ", text)))


REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"

# Any of the dashes a board might use, or the word.
_RANGE = r"(?:\s*(?:-|–|—|~|\bto\b)\s*)"
# The symbol may sit before either number, so both sides must allow one.
_MONEY = r"[$£€]?\s*([\d][\d,]*(?:\.\d+)?)\s*([KkMm])?"

# An annual salary outside this is not a salary: below it lies a 401(k) match
# or an hourly figure, above it a funding round.
_MIN_PLAUSIBLE = 10_000
_MAX_PLAUSIBLE = 2_000_000

_PAY_CONTEXT = re.compile(
    r"\b(salary|compensation|base pay|pay range|pay band|annual|annually|"
    r"per year|/\s*yr|OTE|total target)\b",
    re.I,
)
# Greenhouse and Ashby both stamp the currency on, which is unambiguous enough
# to trust without any surrounding words.
_CURRENCY_SUFFIX = re.compile(r"\b(USD|CAD|GBP|EUR|AUD)\b", re.I)

_HOURLY = re.compile(r"\b(per hour|hourly|/\s*hr|an hour)\b", re.I)


@dataclass(frozen=True)
class Salary:
    low: int
    high: int
    currency: str = "USD"
    period: str = "year"

    def label(self) -> str:
        # A bare "$" on a Canadian or Australian range reads as US dollars,
        # which is a 30% lie. Name any currency that is not USD.
        mark = {"USD": "$", "CAD": "C$", "AUD": "A$", "GBP": "£", "EUR": "€"}.get(
            self.currency, f"{self.currency} "
        )
        if self.period == "hour":
            return f"{mark}{self.low:,}–{mark}{self.high:,}/hr"
        return f"{mark}{round(self.low / 1000)}K–{mark}{round(self.high / 1000)}K"


def _to_number(raw: str, suffix: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def salary(text: str | None) -> Salary | None:
    """The pay range a posting publishes, or None when it publishes none.

    Four formats appear in real boards and all four are handled:
        $285,000—$325,000 USD        $140.8K – $231K • Offers Equity
        $180,000 - $240,000 annually between $135,000 to $145,000
    """
    text = _normalise(text)
    if not text:
        return None

    pattern = re.compile(_MONEY + _RANGE + _MONEY)
    for match in pattern.finditer(text):
        low = _to_number(match.group(1), match.group(2))
        high = _to_number(match.group(3), match.group(4))
        if low is None or high is None or low > high:
            continue

        window = text[max(0, match.start() - 140): match.end() + 60]
        hourly = bool(_HOURLY.search(window))
        if hourly:
            # An hourly rate is real pay, just on a different clock.
            if not (5 <= low <= 500 and 5 <= high <= 500):
                continue
            return Salary(int(low), int(high), _currency(window), "hour")

        if not (_MIN_PLAUSIBLE <= low <= _MAX_PLAUSIBLE):
            continue
        if not (_MIN_PLAUSIBLE <= high <= _MAX_PLAUSIBLE):
            continue
        # Three ways to know this is money: the board stamped a currency code
        # on it, a symbol is attached to the numbers themselves, or something
        # nearby calls it pay. Without one, it is just a pair of numbers.
        stamped = _CURRENCY_SUFFIX.search(window)
        symbol = any(mark in match.group(0) for mark in ("$", "£", "€"))
        if not (stamped or symbol or _PAY_CONTEXT.search(window)):
            continue
        return Salary(int(low), int(high), _currency(window), "year")
    return None


def _currency(window: str) -> str:
    found = _CURRENCY_SUFFIX.search(window)
    if found:
        return found.group(1).upper()
    if "£" in window:
        return "GBP"
    if "€" in window:
        return "EUR"
    return "USD"


# Years must sit next to something that makes them a requirement; a company
# "celebrating 10 years" is not asking for a decade of experience.
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*(?:\+\s*)?years?\b",
    re.I,
)
# How a company talks about ITSELF. "With more than 40 years of experience in
# the industry" satisfies every positive cue and is not a requirement at all.
_COMPANY_HISTORY = re.compile(
    r"\b(?:with (?:more than|over|nearly|almost)|for (?:more than|over|nearly)|"
    r"we(?:'ve| have) been|founded|established|celebrating|history of|legacy of|"
    r"combined|collectively|team (?:has|with)|our (?:team|founders|leadership))\b",
    re.I,
)
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(experience|experienced|background|working|work|building|managing|"
    r"developing|engineering|professional|industry|hands[- ]on|expertise|"
    r"practice|career|minimum|at least|required|proven)\b",
    re.I,
)


def experience_years(text: str | None) -> tuple[int | None, int | None]:
    """(minimum, maximum) years a posting asks for; (None, None) if unstated.

    "5+ years" is (5, None). "3-5 years" is (3, 5).

    A posting states several bars — "4+ years in DevOps" and "2+ years with
    Kubernetes" — and you must clear all of them, so the binding requirement is
    the HIGHEST of the stated minimums, not the lowest. Reporting the lowest
    made a role wanting four years look like it wanted two.
    """
    text = _normalise(text)
    if not text:
        return None, None

    best_low: int | None = None
    best_high: int | None = None
    for match in _YEARS.finditer(text):
        window = text[max(0, match.start() - 90): match.end() + 90]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        # Look only at what comes BEFORE: "With more than 40 years" is the
        # company describing itself, and the giveaway is always the lead-in.
        if _COMPANY_HISTORY.search(text[max(0, match.start() - 60): match.start()]):
            continue
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else None
        # Nobody requires a quarter-century; past this it is prose about
        # something else that slipped the other guards.
        if not (0 < low <= 25):
            continue
        if high is not None and not (low <= high <= 40):
            high = None
        if best_low is None or low > best_low:
            best_low, best_high = low, high
    return best_low, best_high


# "Remote-friendly" and "remote-first" describe a company's culture, not where
# this job is done, so they must not read as remote.
_NOT_REALLY_REMOTE = re.compile(r"remote[- ](?:friendly|first|ish)", re.I)
_REMOTE = re.compile(r"\bremote\b|\banywhere\b|\bdistributed\b|\bwork from home\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\bflexible\b", re.I)
_ONSITE = re.compile(r"\bon-?site\b|\bin[- ]office\b|\bin[- ]person\b", re.I)
# A place needs more than a country to mean "come to this office".
_A_PLACE = re.compile(r"[A-Za-z]+\s*,\s*[A-Za-z]", re.I)


def workplace(
    location: str | None,
    title: str | None = None,
    *,
    is_remote: bool | None = None,
    workplace_type: str | None = None,
) -> str | None:
    """remote / hybrid / onsite, or None when the posting does not make it clear.

    Read from the LOCATION, never the description: descriptions are full of
    "we're a remote-first company" boilerplate that says nothing about the job.
    Ashby publishes this as structured fields, so prefer those when given.
    """
    if workplace_type:
        known = workplace_type.strip().lower()
        if "hybrid" in known:
            return HYBRID
        if "remote" in known:
            return REMOTE
        if known in ("onsite", "on-site", "on site", "in office", "in-office"):
            return ONSITE
    if is_remote is True:
        return REMOTE

    text = _normalise(" ".join(part for part in (location, title) if part)).strip()
    if not text:
        return None

    hybrid = bool(_HYBRID.search(text))
    onsite = bool(_ONSITE.search(text))
    cleaned = _NOT_REALLY_REMOTE.sub(" ", text)
    remote = bool(_REMOTE.search(cleaned))

    # A posting listing several arrangements is offering a choice, and hybrid
    # is the honest summary of "some days here, some days not".
    if hybrid or (remote and onsite):
        return HYBRID
    if remote:
        return REMOTE
    if onsite or _A_PLACE.search(text):
        return ONSITE
    # A bare country is not an office. Absence of "remote" is not evidence.
    return None


# -- rendering --------------------------------------------------------------
# Blank would be ambiguous between "the posting was silent" and "nobody has
# looked yet". These say which.

UNSTATED = "not published"


def salary_label(low: int | None, high: int | None,
                 currency: str | None = "USD", period: str | None = "year") -> str:
    if low is None or high is None:
        return UNSTATED
    return Salary(low, high, currency or "USD", period or "year").label()


def experience_label(low: int | None, high: int | None) -> str:
    if low is None:
        return UNSTATED
    if high:
        return f"{low}–{high} yrs"
    return f"{low}+ yrs"


def workplace_label(value: str | None) -> str:
    return value or UNSTATED


# -- what kind of job ---------------------------------------------------------------------------

JOB_TYPES = {"full-time": "Full-time", "contract": "Contract", "contract-to-hire": "Contract-to-hire",
             "part-time": "Part-time", "temporary": "Temporary", "internship": "Internship"}
JOB_TYPE_SHORT = {"full-time": "Full-time", "contract": "Contract", "contract-to-hire": "C2H",
                  "part-time": "Part-time", "temporary": "Temp", "internship": "Intern"}
# A contract of any kind outranks full-time: "a full-time contract" is a contract.
_TYPE_ORDER = ("contract-to-hire", "contract", "temporary", "internship", "part-time", "full-time")

# The patterns below read text whose blanks are already one space or one line break (_normalise),
# and every gap in them is one whitespace character or a short, bounded class -- never two
# open-ended runs side by side. With those ("\d\s*\+?\s*-?\s*months"), a posting carrying a long run
# of blanks took minutes to read: the matcher tried every way of sharing the run between them.
_C2H = (r"\bcontract[\s-]{1,3}to[\s-]{1,3}(?:hire|perm(?:anent)?)\b|\b(?:c2h|cth)\b"
        r"|\btemp(?:orary)?[\s-]{1,3}to[\s-]{1,3}(?:hire|perm(?:anent)?)\b")
# "Contract Manager", "Contract Specialist": a job about contracts, not a contract job.
_CONTRACT_WORK = (r"(?:manager|management|specialist|administrator|admin|analyst|negotiator|officer|coordinator|"
                  r"compliance|attorney|lawyer|counsel|paralegal|reviewer|review|pricing|lead|associate|director|"
                  r"closeout|accountant|operations|support|assistant|clerk)")
# "(no C2C, no 1099, no third-party)", "not a contract role": what a posting says it is not.
_NEGATED = re.compile(r"\b(?:no|not|non|without|excluding)[\s-]{1,3}(?:an?\s|any\s)?(?:c2c|corp[\s-]to[\s-]corp|1099|"
                      r"contract(?:ors?|s)?|third[\s-]part(?:y|ies)|w-?2)\b", re.I)
_TITLE_RULES = (
    ("contract-to-hire", re.compile(_C2H, re.I)),
    ("contract", re.compile(r"\b(?:c2c|corp[\s-]to[\s-]corp|1099)\b", re.I)),
    # Staffing firms' "W2 Role - Data Engineer" is a contract; "Engineer (Full-time W2)" is not.
    ("contract", re.compile(r"^(?!.*\b(?:full[\s-]?time|permanent|fte)\b).*\bw-?2\b", re.I)),
    ("contract", re.compile(rf"\bcontract(?:or)?\b(?!\s{_CONTRACT_WORK}\b)", re.I)),
    ("temporary", re.compile(r"\b(?:temp|temporary|seasonal)\b", re.I)),
    ("internship", re.compile(r"\bintern(?:ship)?s?\b", re.I)),
    ("part-time", re.compile(r"\bpart[\s-]?time\b", re.I)),
    ("full-time", re.compile(r"\bfull[\s-]?time\b|\bpermanent\b", re.I)),
)
# "Employment type: Contract", "Job type: Full-time" (job alerts), "Position type: Contract to hire".
_LABELLED = re.compile(r"\b(?:employment|job|position|work|worker|contract|role|engagement)[\s-]?"
                       r"(?:type|status|category)\s?[:\-–]\s?([^\n|;•]{2,40})", re.I)
_TEXT_RULES = (
    ("contract-to-hire", re.compile(_C2H + r"|\bright[\s-]{1,3}to[\s-]{1,3}hire\b", re.I)),
    ("contract", re.compile(
        r"\b\d{1,2}[\s+-]{0,4}(?:months?|mos?|weeks?)\b[^.\n]{0,25}?\bcontract\b"        # "6-month contract"
        # Not "contract period" or "contract term", nor "work on a contract": in a defense posting those
        # are the employer's government contract ("the contract period of performance").
        r"|\bcontract\s(?:role|position|assignment|engagement|opportunity|basis|job|duration|length)\b"
        r"|\bthis\sis\san?\s(?:[\w-]{1,40}\s){0,3}contract(?:\s(?:role|position|job|assignment|opportunity|"
        r"engagement|gig)\b|\s?[.,;!)]|\swith\b)"
        r"|\b(?:c2c|corp[\s-]to[\s-]corp)\b|\b1099\s(?:contract|role|position|basis)\b"
        r"|(?<!direct\s)\bw-?2\s(?:contract|role)\b"          # not "a full-time, direct W2 position"
        r"|\bduration\s?[:\-–]\s?\d{1,3}[\s+]{0,3}(?:months?|weeks?|years?)\b", re.I)),
    ("temporary", re.compile(r"\btemporary\s(?:role|position|assignment|job|opportunity|basis)\b"
                             r"|\bthis\sis\sa\stemporary\b", re.I)),
    ("internship", re.compile(r"\bthis\sis\san?\s(?:[\w-]{1,40}\s){0,2}internship\b"
                              r"|\b(?:summer|fall|spring|winter|paid)\sinternship\b", re.I)),
    ("part-time", re.compile(r"\bthis\sis\sa\spart[\s-]?time\b"
                             r"|\bpart[\s-]?time\s(?:role|position|job|opportunity|basis|schedule)\b", re.I)),
    ("full-time", re.compile(r"\bthis\s(?:is|will\sbe)\san?\s(?:[\w-]{1,40}\s){0,2}full[\s-]?time\b"
                             r"|\bfull[\s-]?time\s(?:role|position|job|opportunity|employment|employee|permanent|"
                             r"salaried|exempt|w-?2|offers?)\b|\bpermanent\s(?:role|position|job|full[\s-]?time)\b"
                             # "the salary range for full-time employees for this position", "benefits for
                             # full-time employees", "full-time employees are eligible / also receive"
                             r"|\b(?:salary|pay|compensation|wage)\b[^.\n]{0,40}\bfor\sfull[\s-]?time\b"
                             r"|\bbenefits?\b[^.\n]{0,60}\bfull[\s-]?time\semployees\b"
                             r"|\bfull[\s-]?time\semployees\s(?:are|also|have|receive|get|enjoy|can|may)\b"
                             r"|\bfull[\s-]?time[\s,]{1,3}(?:direct\s)?w-?2\b", re.I)),     # "full-time, direct W2"
)
def type_of(label: str | None) -> str | None:
    """A kind of job from a board's own field or a labelled line: "Full-time", "FullTime",
    "Contract to hire", "Contractor", "Temp", "Intern", "Permanent", "Contingent Worker"."""
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label or "")           # "FullTime" -> "Full Time"
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    if not t:
        return None
    if re.search(r"\bcontract (?:to|2) (?:hire|perm)|\bc2h\b|\bcth\b|\btemp (?:to|2) (?:hire|perm)|\bright to hire\b", t):
        return "contract-to-hire"
    if re.search(r"\b(?:contract|contractor|c2c|corp to corp|1099|fixed term|freelance|contingent|consultant)\b", t):
        return "contract"
    if re.search(r"\b(?:temp|temporary|seasonal)\b", t):
        return "temporary"
    if re.search(r"\bintern(?:ship)?\b", t):
        return "internship"
    if re.search(r"\bpart ?time\b", t):
        return "part-time"
    if re.search(r"\bfull ?time\b|\bpermanent\b|\bregular\b|\bemployee\b", t):
        return "full-time"
    return None


def job_type(title: str | None, description: str | None, requirements: str | None = None,
             stated: str | None = None) -> str | None:
    """What kind of job a posting is, from what it states: its title, its board's own field,
    a labelled line ("Employment type: Contract") and its wording ("a 6-month contract"). A
    contract of any kind outranks full-time. Nothing stated is None -- never a guess."""
    heading = _NEGATED.sub(" ", _normalise(title)[:300])
    found: set[str | None] = {kind for kind, rule in _TITLE_RULES if rule.search(heading)}
    found.add(type_of(stated))
    text = _NEGATED.sub(" ", _normalise("\n".join(part for part in (description, requirements) if part)[:30000]))
    found.update(type_of(m.group(1)) for m in _LABELLED.finditer(text))
    found.update(kind for kind, rule in _TEXT_RULES if rule.search(text))
    return next((kind for kind in _TYPE_ORDER if kind in found), None)


def derive(title: str | None, location: str | None,
           description: str | None, requirements: str | None,
           stated_job_type: str | None = None) -> dict[str, object]:
    """Every derived field for one posting, as column -> value.

    One function so the scrape and the backfill cannot drift: a parser fix has
    to reach both, and two copies of this logic would eventually disagree about
    the same posting.
    """
    # Requirements first — that is where a posting states its bar. The full
    # description is the fallback, since only about 40% carry a parsed
    # requirements section.
    experience_text = "\n".join(part for part in (requirements, description) if part)
    low, high = experience_years(experience_text)
    pay = salary(description)
    return {
        "workplace": workplace(location, title),
        "job_type": job_type(title, description, requirements, stated_job_type),
        "experience_min_years": low,
        "experience_max_years": high,
        "salary_min": pay.low if pay else None,
        "salary_max": pay.high if pay else None,
        "salary_currency": pay.currency if pay else None,
        "salary_period": pay.period if pay else None,
        **_eligibility(title, description, requirements),
    }


ELIGIBILITY_FIELDS = ("clearance", "clearance_active", "polygraph", "citizenship", "sponsorship")


def derived_for(job, fields: dict) -> dict:
    """The derived fields to write to an existing job: the person's own corrections -- clearance,
    citizenship, visa, and the kind of job -- are never overwritten."""
    if getattr(job, "eligibility_manual", None):
        fields = {k: v for k, v in fields.items() if k not in ELIGIBILITY_FIELDS}
    if getattr(job, "job_type_manual", None):
        fields = {k: v for k, v in fields.items() if k != "job_type"}
    return fields


def _eligibility(title, description, requirements) -> dict:
    from backend.core.eligibility import parse
    return parse(title, description, requirements)
