"""The company's own page for a job, added by the person on the job's page (25 Oct 2026).

Two kinds, kept apart because only one of them is a form to apply on:

  posting   this job's own page on the company's site (careers.oracle.com/.../job/348662):
            Auto-apply and "Open the application form" go there
  careers   the careers site itself, or a search on it: kept as a link to look from, never
            taken for the application form -- Auto-apply goes on through the job site's link

The kind is read from the link (`kind`), and the person can say otherwise on the job's page.
Unsure, it is `careers`: a front page opened as though it were the form fills nothing, while
the job site's link at least reaches the posting.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, unquote, urlsplit

POSTING, CAREERS = "posting", "careers"
KINDS = (POSTING, CAREERS)
MAX_LENGTH = 1000

# Query keys that name one job: Greenhouse's gh_jid, Taleo's job, Workday's and others' jobId...
_JOB_KEYS = {"gh_jid", "job", "jobid", "job_id", "jid", "reqid", "req_id", "requisitionid", "requisition_id",
             "jobreqid", "jobpostingid", "postingid", "vacancyid", "id", "pid", "oid"}
# Path words a job's own id follows: Workable's /j/<id>, LinkedIn's /jobs/view/<id>...
_BEFORE_ID = {"j", "job", "jobs", "position", "positions", "opening", "openings", "posting", "postings",
              "requisition", "requisitions", "vacancy", "vacancies", "details", "view", "apply"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUMBER = re.compile(r"\d{4,}")                       # 348662, iCIMS's 1234
_YEAR = re.compile(r"(19|20)\d\d")                    # /careers/2024 is no job
_NUMBERED_TITLE = re.compile(r"\d{5,}[-_].+|.+[-_][a-z]{0,4}\d{4,}", re.I)   # Google's 1234567-sre, Workday's SRE_R12345


class NotALink(ValueError):
    """Why a pasted link cannot be kept, in words the person can act on."""


def clean(raw: str) -> str:
    """The link as kept, or "" to take it off. A web link only: http or https, to a named site on
    the internet -- never a script, a file, this computer or a private network, and never a name
    and password inside the link."""
    url = raw.strip()
    if not url:
        return ""
    if len(url) > MAX_LENGTH:
        raise NotALink("That link is too long to be a job's page.")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise NotALink("That is not one link: it has spaces in it.")
    scheme = re.match(r"([a-z][a-z0-9+-]*):", url, re.I)      # no dot: careers.example.com:8080 is a host
    if scheme and not url[scheme.end():].startswith("//") and scheme.group(1).lower() not in ("http", "https"):
        raise NotALink("Only web links can be kept: paste the address, like https://careers.example.com/jobs/12345")
    if "://" not in url:
        url = "https://" + url                         # pasted as careers.example.com/...
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port                                     # noqa: B018 -- a bad port raises here
    except ValueError:
        raise NotALink("That is not a web link: paste the whole address.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise NotALink("Only web links can be kept: paste the address, like https://careers.example.com/jobs/12345")
    if parts.username or parts.password:
        raise NotALink("A link with a name or password in it can't be kept: paste the page's own address.")
    if not host or host == "localhost" or host.endswith((".local", ".localhost")):
        raise NotALink("That link points at this computer, not at a company's site.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise NotALink("That link points at this computer or a private network, not at a company's site.")
    if address is None and "." not in host:
        raise NotALink("That is not a web link: paste the whole address, like https://careers.example.com/jobs/12345")
    return url


def kind(url: str) -> str:
    """POSTING when the link names one job (an id in its path, after its #, or in its query), else CAREERS."""
    parts = urlsplit(url)
    for key, values in parse_qs(parts.query).items():
        if key.lower() in _JOB_KEYS and any(re.search(r"\d", v) for v in values):
            return POSTING
    segments = [s for s in re.split(r"[/#?&]", unquote(parts.path + "#" + parts.fragment)) if s]
    for before, segment in zip([""] + segments, segments):
        if _UUID.search(segment) or _NUMBERED_TITLE.fullmatch(segment):
            return POSTING
        if _NUMBER.fullmatch(segment) and not _YEAR.fullmatch(segment):
            return POSTING
        if before.lower() in _BEFORE_ID and (re.fullmatch(r"\d{3,}", segment) and not _YEAR.fullmatch(segment)
                                             or len(segment) >= 5 and len(re.findall(r"\d", segment)) >= 2):
            return POSTING                             # /jobs/512, Workable's /j/1A2B3C4D5E
    return CAREERS


def apply_link(job) -> str:
    """Where to apply: the company's own posting when the person added one, else the job site's link.
    A careers site added is never taken for the form."""
    own = getattr(job, "employer_url", None)
    if own and (getattr(job, "employer_url_kind", None) or kind(own)) == POSTING:
        return own
    return job.application_url or ""
