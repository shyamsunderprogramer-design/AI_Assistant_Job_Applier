"""What a careers page looks like, and which hiring system sits behind it.

Pure functions, no requests: `careers_finder.py` does the fetching and calls
these to read what came back. Kept apart so every decision that sends the
finder somewhere -- which link is the careers link, whose board a URL names,
which of seven same-named companies a domain belongs to -- can be tested
against a saved page without touching the network.

Three questions, answered from text alone:

  * **Whose board is this URL?**  `detect_url` knows the URL shapes of about
    sixty hiring systems and pulls the board name (slug) out of the ones that
    carry it -- `boards.greenhouse.io/acme`, `acme.wd5.myworkdayjobs.com/External`,
    `recruiting.ultipro.com/ACM1000/JobBoard/<uuid>`. A careers page that embeds
    or links its board is read the same way: every URL in the markup is tried.
  * **Which link on a homepage is the careers link?**  `score_link` weighs the
    link text ("Careers", "Join us", "Werken bij") against the address
    (`/careers`, `jobs.acme.com`) and against the traps: a blog post about
    careers, a "Find a job" link on a staffing site, LinkedIn.
  * **Which company is "Sierra"?**  `pick_domain` chooses among the dump's
    same-named companies only when one clearly stands out, and otherwise says
    it does not know -- a wrong domain sends every later request to the wrong
    company, which is worse than no answer.
"""

from __future__ import annotations

import html as _html
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# ---------------------------------------------------------------------------
# Hosts and domains
# ---------------------------------------------------------------------------

# Two-label public suffixes: "acme.co.uk" is one company, not all of ".co.uk".
# Not the whole Public Suffix List -- the ones these company lists contain.
_TWO_LABEL_SUFFIXES = frozenset("""
co.uk org.uk ac.uk gov.uk ltd.uk plc.uk me.uk net.uk nhs.uk
com.au net.au org.au edu.au gov.au co.nz org.nz net.nz govt.nz
co.jp ne.jp or.jp ac.jp co.in net.in org.in firm.in gen.in ac.in edu.in gov.in
co.za org.za com.br net.br org.br gov.br com.mx org.mx gob.mx com.sg edu.sg gov.sg
com.hk org.hk com.cn net.cn org.cn com.tw org.tw co.kr or.kr com.tr com.ar co.il org.il
com.my com.ph com.vn com.ng co.id com.co com.pe com.ec com.pk com.bd com.eg com.sa
com.qa co.ke com.gh com.ua com.pl co.th in.th com.uy com.ve co.at or.at com.es com.pt
com.gr com.cy com.mt com.np com.lk com.kw com.bh com.om co.ae ac.ae gov.ae com.do
""".split())

# Hosting platforms that give each customer a subdomain: there the customer is
# the third label, not the second.
_SHARED_HOSTS = frozenset("""
github.io wixsite.com squarespace.com webflow.io herokuapp.com netlify.app vercel.app
wordpress.com blogspot.com myshopify.com weebly.com godaddysites.com carrd.co
notion.site framer.website framer.app azurewebsites.net firebaseapp.com web.app pages.dev
""".split())

_DOMAIN_SHAPE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def host_of(url: str | None) -> str:
    """Lower-case host without port; "" when there is none."""
    if not url:
        return ""
    if "://" not in url and not url.startswith("//"):
        url = "//" + url
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def registrable_domain(value: str | None) -> str | None:
    """'https://careers.acme.co.uk/jobs' -> 'acme.co.uk'. None when there is none."""
    host = host_of(value)
    if host.startswith("www."):
        host = host[4:]
    if not host or _IPV4.match(host):
        return None
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    width = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES | _SHARED_HOSTS else 2
    if len(labels) < width:
        return None
    return ".".join(labels[-width:])


def clean_domain(value: str | None) -> str | None:
    """A list's domain cell -> a bare domain, or None when it is not one.

    The dump carries its own typos -- "www,dvc.com", "firstheritage,.net" -- so a
    comma is read as the dot it was meant to be, as refine.py does.
    """
    text = (value or "").strip().lower().replace(",", ".")
    if not text:
        return None
    host = host_of(text)
    host = ".".join(part for part in host.split(".") if part)
    if host.startswith("www."):
        host = host[4:]
    if not _DOMAIN_SHAPE.fullmatch(host) or len(host) > 253:
        return None
    if any(len(label) > 63 for label in host.split(".")):
        return None             # a DNS label over 63 octets raises inside idna
    return host


def domain_root(domain: str | None) -> str:
    """The name part of a domain: 'acme' for 'careers.acme.co.uk'."""
    reg = registrable_domain(domain or "")
    if not reg:
        return ""
    labels = reg.split(".")
    suffix_width = 2 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 1
    return labels[-suffix_width - 1] if len(labels) > suffix_width else labels[0]


# ---------------------------------------------------------------------------
# Hiring systems
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hit:
    """A hiring system seen in a URL: which one, whose board, and where."""

    system: str
    slug: str | None
    board_url: str | None
    seen_at: str


# Subdomains a vendor uses for itself, never a customer's board.
_VENDOR_LABELS = frozenset("""
www app apps api assets static cdn cdn2 img images media files help support status docs
developer developers blog marketing partner partners login signin sso auth mail email go
info resources community demo sandbox staging test feedback attachments gallery uploads
""".split())

# Path words that sit where a slug would, but are the vendor's own pages.
_NOT_SLUGS = frozenset("""
embed v0 v1 v2 v3 api j jobs job careers career search apply login signin assets static
css js img images favicon.ico robots.txt sitemap.xml privacy terms about cdn-cgi wp-content
en en-us u widget widgets public portal home index.html
""".split())


def _query(url_parts, key: str) -> str | None:
    """A query parameter, case-insensitively by name."""
    for name, values in parse_qs(url_parts.query, keep_blank_values=False).items():
        if name.lower() == key.lower() and values and values[0].strip():
            return values[0].strip()
    return None


def _workday(parts, host: str, path: str) -> tuple[str, str] | None:
    from data_engineering.scraper.workday import make_slug, slug_from_url

    m = re.match(r"^([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        tenant, dc = m.group(1), m.group(2)
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 4 and segs[0] == "wday" and segs[1] == "cxs":
            return make_slug(segs[2], dc, segs[3]), f"https://{tenant}.{dc}.myworkdayjobs.com/{segs[3]}"
        slug = slug_from_url(f"https://{host}{path}")
        if slug:
            site = slug.split(":")[2]
            if site.lower() not in ("wday", "login", "job", "jobs", "apply"):
                return slug, f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"
        return "", f"https://{tenant}.{dc}.myworkdayjobs.com/"
    m = re.match(r"^(?:www\.)?(wd\d+)\.myworkdaysite\.com$", host)
    if m:
        segs = [s for s in path.split("/") if s]
        if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
            segs = segs[1:]
        if len(segs) >= 3 and segs[0].lower() == "recruiting":
            dc = m.group(1)
            return make_slug(segs[1], dc, segs[2]), f"https://{segs[1]}.{dc}.myworkdayjobs.com/{segs[2]}"
        return "", None
    return None


# (system, pattern over "host/path?query", board-url template or None)
# The pattern's named groups fill the template; group "s" is the slug. Order
# matters only within one system: the specific shape comes before the generic.
_R = re.IGNORECASE
_RULES: list[tuple[str, re.Pattern, str | None]] = [(system, re.compile(pattern, _R), board) for system, pattern, board in [
    ("greenhouse", r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^#]*&)?for=(?P<s>[\w.-]+)", "https://job-boards.greenhouse.io/{s}"),
    ("greenhouse", r"^app\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^#]*&)?for=(?P<s>[\w.-]+)", "https://job-boards.greenhouse.io/{s}"),
    ("greenhouse", r"^(?:boards-)?api\.greenhouse\.io/v1/boards/(?P<s>[\w.-]+)", "https://job-boards.greenhouse.io/{s}"),
    ("greenhouse", r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?P<s>[\w.-]+)", "https://job-boards.greenhouse.io/{s}"),
    ("greenhouse", r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io(?:/|$)", None),
    ("greenhouse", r"^grnh\.se/", None),
    ("lever", r"^api(?:\.eu)?\.lever\.co/v0/postings/(?P<s>[\w.-]+)", "https://jobs.lever.co/{s}"),
    ("lever", r"^jobs(?:\.eu)?\.lever\.co/(?P<s>[\w.-]+)", "https://jobs.lever.co/{s}"),
    ("ashby", r"^api\.ashbyhq\.com/posting-api/job-board/(?P<s>[\w.%-]+)", "https://jobs.ashbyhq.com/{s}"),
    ("ashby", r"^jobs\.ashbyhq\.com/(?P<s>[\w.%-]+)", "https://jobs.ashbyhq.com/{s}"),
    ("workable", r"^(?:apply|www)\.workable\.com/api/v\d/(?:widget/)?accounts/(?P<s>[\w-]+)", "https://apply.workable.com/{s}/"),
    ("workable", r"^www\.workable\.com/api/accounts/(?P<s>[\w-]+)", "https://apply.workable.com/{s}/"),
    ("workable", r"^apply\.workable\.com/(?P<s>[\w-]+)", "https://apply.workable.com/{s}/"),
    ("workable", r"^(?P<s>[\w-]+)\.workable\.com(?:/|$)", "https://apply.workable.com/{s}/"),
    ("workable", r"^apply\.workable\.com/", None),
    ("jazzhr", r"^(?P<s>[\w-]+)\.applytojob\.com(?:/|$)", "https://{s}.applytojob.com/apply"),
    ("jazzhr", r"^app\.jazz\.co/widgets/", None),
    ("bamboohr", r"^(?P<s>[\w-]+)\.bamboohr\.com(?:/|$)", "https://{s}.bamboohr.com/careers"),
    ("breezy", r"^(?P<s>[\w-]+)\.breezy\.hr(?:/|$)", "https://{s}.breezy.hr/"),
    ("recruitee", r"^(?P<s>[\w-]+)\.recruitee\.com(?:/|$)", "https://{s}.recruitee.com/"),
    ("teamtailor", r"^(?P<s>[\w-]+)\.teamtailor\.com(?:/|$)", "https://{s}.teamtailor.com/jobs"),
    ("smartrecruiters", r"^api\.smartrecruiters\.com/v1/companies/(?P<s>[\w-]+)", "https://careers.smartrecruiters.com/{s}"),
    ("smartrecruiters", r"^(?:careers|jobs)\.smartrecruiters\.com/(?P<s>[\w-]+)", "https://careers.smartrecruiters.com/{s}"),
    ("jobvite", r"^jobs\.jobvite\.com/careers/(?P<s>[\w-]+)", "https://jobs.jobvite.com/{s}/jobs"),
    ("jobvite", r"^(?:jobs|careers)\.jobvite\.com/(?P<s>[\w-]+)", "https://jobs.jobvite.com/{s}/jobs"),
    ("jobvite", r"^(?:app|hire)\.jobvite\.com/CompanyJobs/", None),
    ("icims", r"^(?P<s>[\w-]+)\.icims\.com(?:/|$)", "https://{s}.icims.com/jobs"),
    ("adp", r"^workforcenow\.adp\.com/.*[?&]cid=(?P<s>[\w-]+)", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={s}"),
    ("adp", r"^myjobs\.adp\.com/(?P<s>[\w-]+)", "https://myjobs.adp.com/{s}/cx/job-listing"),
    ("adp", r"^recruiting\.adp\.com/srccar/public/RTI\.home\?(?:[^#]*&)?c=(?P<s>\d+)", "https://recruiting.adp.com/srccar/public/RTI.home?c={s}"),
    ("adp", r"^(?:workforcenow|myjobs|recruiting)\.adp\.com/", None),
    ("ultipro", r"^(?P<h>recruiting2?\.ultipro\.com|[\w-]+\.rec\.pro\.ukg\.net)/(?P<t>[A-Z0-9]+)/JobBoard/(?P<u>[0-9a-f-]{36})", "https://{h}/{t}/JobBoard/{u}/"),
    ("ultipro", r"^(?:recruiting2?\.ultipro\.com|[\w-]+\.rec\.pro\.ukg\.net)/", None),
    ("paycom", r"^(?:www\.)?paycomonline\.net/v4/ats/web\.php/jobs.*[?&]clientkey=(?P<s>\w+)", "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={s}"),
    ("paycom", r"^(?:www\.)?paycomonline\.net/v4/ats/", None),
    ("paylocity", r"^recruiting\.paylocity\.com/recruiting/jobs/all/(?P<s>[0-9a-f-]{36})", "https://recruiting.paylocity.com/recruiting/jobs/All/{s}"),
    ("paylocity", r"^recruiting\.paylocity\.com/", None),
    ("dayforce", r"^jobs\.dayforcehcm\.com/(?:[a-z]{2}-[a-z]{2}/)?(?P<s>(?!api\b|candidateportal\b)[\w-]+)", "https://jobs.dayforcehcm.com/en-US/{s}/CANDIDATEPORTAL"),
    ("dayforce", r"^[\w-]+\.dayforcehcm\.com/candidateportal/(?:[a-z]{2}-[a-z]{2}/)?(?P<s>[\w-]+)", "https://jobs.dayforcehcm.com/en-US/{s}/CANDIDATEPORTAL"),
    ("taleo", r"^(?P<s>[\w-]+)\.taleo\.net/careersection/(?P<c>[\w-]+)", "https://{s}.taleo.net/careersection/{c}/jobsearch.ftl"),
    ("taleo", r"^tbe\.taleo\.net/.*[?&]org=(?P<s>\w+)", None),
    ("taleo", r"^(?P<s>(?!tbe\b)[\w-]+)\.taleo\.net(?:/|$)", "https://{s}.taleo.net/careersection/jobsearch.ftl"),
    ("successfactors", r"^(?P<h>career\d*\.successfactors\.(?:com|eu))/.*[?&]company=(?P<s>[\w-]+)", "https://{h}/career?company={s}"),
    ("successfactors", r"^(?P<s>[\w-]+)\.jobs2web\.com(?:/|$)", None),
    ("successfactors", r"^career\d*\.successfactors\.(?:com|eu)/", None),
    ("personio", r"^(?P<s>[\w-]+)\.jobs\.personio\.(?P<tld>de|com)(?:/|$)", "https://{s}.jobs.personio.{tld}/"),
    ("rippling", r"^ats\.rippling\.com/(?:[a-z]{2}-[a-z]{2}/)?(?P<s>[\w-]+)", "https://ats.rippling.com/{s}/jobs"),
    ("oracle", r"^(?P<h>(?P<p>[\w-]+)\.fa\.[\w-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/(?P<site>[\w-]+)", "https://{h}/hcmUI/CandidateExperience/en/sites/{site}"),
    ("oracle", r"^[\w-]+\.fa\.[\w-]+\.oraclecloud\.com/hcmUI/CandidateExperience", None),
    ("pinpoint", r"^(?P<s>[\w-]+)\.pinpointhq\.com(?:/|$)", "https://{s}.pinpointhq.com/"),
    ("avature", r"^(?P<s>[\w-]+)\.avature\.net(?:/|$)", "https://{s}.avature.net/careers"),
    ("eightfold", r"^(?P<s>[\w-]+)\.eightfold\.ai(?:/|$)", "https://{s}.eightfold.ai/careers"),
    ("phenom", r"^(?P<s>[\w-]+)\.phenompeople\.com(?:/|$)", None),
    ("neogov", r"^(?:www\.)?(?P<d>governmentjobs|schooljobs)\.com/careers/(?P<s>[\w-]+)", "https://www.{d}.com/careers/{s}"),
    ("peopleadmin", r"^(?P<s>[\w-]+)\.peopleadmin\.com(?:/|$)", "https://{s}.peopleadmin.com/"),
    ("applicantpro", r"^(?P<s>[\w-]+)\.applicantpro\.com(?:/|$)", "https://{s}.applicantpro.com/jobs/"),
    ("hiringthing", r"^(?P<s>[\w-]+)\.hiringthing\.com(?:/|$)", "https://{s}.hiringthing.com/"),
    ("careerplug", r"^(?P<s>[\w-]+)\.careerplug\.com(?:/|$)", "https://{s}.careerplug.com/jobs"),
    ("zohorecruit", r"^(?P<s>[\w-]+)\.zohorecruit\.(?P<tld>com|eu|in|com\.au)(?:/|$)", "https://{s}.zohorecruit.{tld}/jobs/Careers"),
    ("freshteam", r"^(?P<s>[\w-]+)\.freshteam\.com(?:/|$)", "https://{s}.freshteam.com/jobs"),
    ("comeet", r"^(?:www\.)?comeet\.com/jobs/(?P<s>[\w-]+)", "https://www.comeet.com/jobs/{s}"),
    ("homerun", r"^(?P<s>[\w-]+)\.homerun\.co(?:/|$)", "https://{s}.homerun.co/"),
    ("gem", r"^jobs\.gem\.com/(?P<s>[\w-]+)", "https://jobs.gem.com/{s}"),
    ("isolved", r"^(?P<s>[\w-]+)\.isolvedhire\.com(?:/|$)", "https://{s}.isolvedhire.com/jobs/"),
    ("paycor", r"^recruitingbypaycor\.com/career/.*[?&]clientid=(?P<s>\w+)", "https://recruitingbypaycor.com/career/CareerHome.action?clientId={s}"),
    ("cornerstone", r"^(?P<s>[\w-]+)\.csod\.com/ux/ats/careersite/(?P<n>\d+)", "https://{s}.csod.com/ux/ats/careersite/{n}/home?c={s}"),
    ("brassring", r"^sjobs\.brassring\.com/TGnewUI/", None),
    ("hrmdirect", r"^(?P<s>[\w-]+)\.hrmdirect\.com(?:/|$)", "https://{s}.hrmdirect.com/employment/job-openings.php"),
    ("applicantstack", r"^(?P<s>[\w-]+)\.applicantstack\.com(?:/|$)", "https://{s}.applicantstack.com/x/openings"),
    ("trakstar", r"^(?P<s>[\w-]+)\.hire\.trakstar\.com(?:/|$)", "https://{s}.hire.trakstar.com/"),
    ("recruiterbox", r"^(?P<s>[\w-]+)\.recruiterbox\.com(?:/|$)", "https://{s}.recruiterbox.com/"),
    ("jobscore", r"^careers\.jobscore\.com/(?:careers|jobs)/(?P<s>[\w-]+)", "https://careers.jobscore.com/careers/{s}"),
    ("catsone", r"^(?P<s>[\w-]+)\.catsone\.com(?:/|$)", "https://{s}.catsone.com/careers"),
    ("polymer", r"^jobs\.polymer\.co/(?P<s>[\w-]+)", "https://jobs.polymer.co/{s}"),
    ("join", r"^join\.com/companies/(?P<s>[\w-]+)", "https://join.com/companies/{s}"),
    ("welcometothejungle", r"^(?:www\.)?welcometothejungle\.com/[a-z]{2}/companies/(?P<s>[\w-]+)", "https://www.welcometothejungle.com/en/companies/{s}/jobs"),
    ("talentlyft", r"^(?P<s>[\w-]+)\.talentlyft\.com(?:/|$)", "https://{s}.talentlyft.com/"),
    ("manatal", r"^(?:www\.)?careers-page\.com/(?P<s>[\w-]+)", "https://www.careers-page.com/{s}"),
    ("recruiterflow", r"^recruiterflow\.com/(?P<s>[\w-]+)/jobs", "https://recruiterflow.com/{s}/jobs"),
    ("jobdiva", r"^www\d*\.jobdiva\.com/portal/", None),
    ("trinethire", r"^app\.trinethire\.com/companies/(?P<s>[\w-]+)", "https://app.trinethire.com/companies/{s}/jobs"),
    ("hireology", r"^careers\.hireology\.com/(?P<s>[\w-]+)", "https://careers.hireology.com/{s}"),
    ("hirebridge", r"^recruit\.hirebridge\.com/", None),
    ("applicantmanager", r"^(?:www\.)?theapplicantmanager\.com/careers\?(?:[^#]*&)?co=(?P<s>\w+)", "https://www.theapplicantmanager.com/careers?co={s}"),
    ("silkroad", r"^(?P<s>[\w-]+)\.silkroad\.com(?:/|$)", "https://{s}.silkroad.com/"),
    ("applitrack", r"^(?:www\.)?applitrack\.com/(?P<s>[\w-]+)/onlineapp", "https://www.applitrack.com/{s}/onlineapp/"),
    ("gusto", r"^jobs\.gusto\.com/boards/(?P<s>[\w-]+)", "https://jobs.gusto.com/boards/{s}"),
    ("factorial", r"^(?P<s>[\w-]+)\.factorialhr\.(?P<tld>com|es|de|fr|it|pt)(?:/|$)", "https://{s}.factorialhr.{tld}/"),
    ("clearcompany", r"^(?P<s>[\w-]+)\.clearcompany\.com(?:/|$)", "https://{s}.clearcompany.com/careers"),
    ("crelate", r"^jobs\.crelate\.com/portal/(?P<s>[\w-]+)", "https://jobs.crelate.com/portal/{s}"),
    ("keka", r"^(?P<s>[\w-]+)\.keka\.com/careers", "https://{s}.keka.com/careers"),
    ("darwinbox", r"^(?P<s>[\w-]+)\.darwinbox\.(?:in|com)(?:/|$)", None),
    ("wellfound", r"^(?:www\.)?(?:wellfound\.com|angel\.co)/company/(?P<s>[\w-]+)", "https://wellfound.com/company/{s}/jobs"),
    ("workatastartup", r"^(?:www\.)?workatastartup\.com/companies/(?P<s>[\w-]+)", "https://www.workatastartup.com/companies/{s}"),
    ("usajobs", r"^(?:www\.)?usajobs\.gov(?:/|$)", None),
]]

# Seen in a careers page's markup when the board lives on the company's own
# domain and its URLs give no slug: the system is known, the board is the page.
_FINGERPRINTS: list[tuple[str, re.Pattern]] = [
    ("teamtailor", re.compile(r"teamtailor-cdn\.com|content=[\"']Teamtailor", re.I)),
    ("phenom", re.compile(r"cdn\.phenompeople\.com|phenompeople\.com/", re.I)),
    ("clinch", re.compile(r"clinchtalent\.com", re.I)),
    ("successfactors", re.compile(r"rmkcdn\.successfactors\.com|\.jobs2web\.com", re.I)),
    ("workable", re.compile(r"workable\.com/assets/embed\.js|whr_embed\(", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_jid=\d+|greenhouse\.io/embed/job_board", re.I)),
    ("eightfold", re.compile(r"eightfold\.ai/careers|static\.vscdn\.net", re.I)),
    ("avature", re.compile(r"\.avature\.net/", re.I)),
    ("icims", re.compile(r"\.icims\.com/jobs", re.I)),
    ("jobvite", re.compile(r"jobvite\.com/__assets__|jobvite-careers", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/(?:job-widget|widgets)", re.I)),
]

# Query parameters worth keeping when a URL is stored or shown. Everything else
# goes: a tracking link's parameters can identify the person it was sent to,
# and some widget URLs carry API keys.
_KEEP_PARAMS = frozenset({"gh_jid", "for", "cid", "clientkey", "company", "c", "org", "cws", "co",
                          "clientid", "partnerid", "siteid", "a", "lang"})


def clean_url(url: str | None) -> str:
    """A URL safe to store: no fragment, no query except a few naming params."""
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    kept = [f"{k}={v[0]}" for k, v in parse_qs(parts.query).items()
            if k.lower() in _KEEP_PARAMS and v]
    query = ("?" + "&".join(kept)) if kept else ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}{query}"[:500]


def detect_url(url: str | None) -> Hit | None:
    """Which hiring system, and whose board, a single URL points at."""
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        parts = urlparse(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    seen = clean_url(url)

    if "myworkday" in host:
        found = _workday(parts, host, parts.path or "/")
        if found is not None:
            slug, board = found
            return Hit("workday", slug or None, board, seen)

    target = host + (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    first_label = host.split(".")[0]
    for system, pattern, template in _RULES:
        m = pattern.match(target)
        if not m:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v}
        slug = groups.get("s")
        if slug is not None:
            # Kept as the URL spells it ("Acme%20Inc"): that is how boards.csv has it.
            slug = slug.strip(".-")
            is_subdomain_slug = host.startswith(slug.lower() + ".")
            if (is_subdomain_slug and slug.lower() in _VENDOR_LABELS) or \
                    (not is_subdomain_slug and slug.lower() in _NOT_SLUGS) or not slug:
                # "www.bamboohr.com", "apply.workable.com/j/..": the vendor's page.
                # Fall through to a generic rule for the same system, if any.
                continue
        if system == "workable" and first_label in _VENDOR_LABELS | {"apply", "jobs", "resources"} \
                and pattern.pattern.startswith("^(?P<s>"):
            continue
        if system == "ultipro" and "t" in groups:
            slug = f"{groups['t']}/{groups['u']}"
        elif system == "taleo" and "c" in groups and slug:
            slug = f"{slug}/{groups['c']}"
        elif system == "oracle" and "p" in groups:
            slug = f"{groups['p']}:{groups['site']}"
        elif system == "cornerstone" and "n" in groups:
            slug = f"{slug}:{groups['n']}"
        board = template.format(**groups) if template and (slug or not groups) else None
        if template and groups and slug is None:
            board = None
        if system == "taleo" and host == "tbe.taleo.net":
            # Taleo Business Edition: one shared host, the company is the org parameter.
            slug = _query(parts, "org")
            cws = _query(parts, "cws")
            board = (f"https://tbe.taleo.net{parts.path}?org={slug}" + (f"&cws={cws}" if cws else "")
                     if slug else None)
        if system == "usajobs":
            board = "https://www.usajobs.gov/"
        if system == "brassring":
            partner, site = _query(parts, "partnerid"), _query(parts, "siteid")
            if partner and site:
                slug = f"{partner}:{site}"
                board = f"https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid={partner}&siteid={site}"
        if system == "jobvite" and slug is None:
            code = _query(parts, "c")
            if code:
                board = f"https://app.jobvite.com/CompanyJobs/Careers.aspx?c={code}"
        if system == "jobdiva":
            code = _query(parts, "a")
            if code:
                slug, board = code, f"https://www1.jobdiva.com/portal/?a={code}"
        if system == "hirebridge":
            code = _query(parts, "cid")
            if code:
                slug, board = code, f"https://recruit.hirebridge.com/v3/CareerCenter/v2/?cid={code}"
        if system == "successfactors" and slug and "jobs2web" in host:
            board = f"https://{host}/"
        if system == "phenom" and slug:
            board = f"https://{host}/"
        if system == "darwinbox" and slug:
            board = f"https://{host}/ms/candidate/careers"
        return Hit(system, slug or None, board, seen)
    return None


# A URL anywhere in markup: in an href, a script src, an iframe, or a JSON blob
# with its slashes escaped. Scheme-relative "//boards.greenhouse.io/.." counts.
_URL_IN_TEXT = re.compile(
    r"(?:https?:)?//[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d+)?(?:/[^\s\"'<>\\)\]}`]*)?", re.I)
# Embed snippets sometimes name the host without any scheme at all.
_BARE_ATS = re.compile(
    r"(?<![\w./-])(?:[\w-]+\.)*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|"
    r"myworkdayjobs\.com|applytojob\.com|bamboohr\.com|breezy\.hr|recruitee\.com|"
    r"teamtailor\.com|smartrecruiters\.com|icims\.com|jobvite\.com)/[^\s\"'<>\\)\]}`]*", re.I)


def _unescape_markup(text: str) -> str:
    return (text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
            .replace("&amp;", "&").replace("&#x2F;", "/").replace("&#47;", "/"))


def urls_in(text: str, limit: int = 4000) -> list[str]:
    """Every URL-looking string in a page, scheme added where it was left off."""
    text = _unescape_markup(text or "")
    found: list[str] = []
    for m in _URL_IN_TEXT.finditer(text):
        url = m.group(0)
        found.append("https:" + url if url.startswith("//") else url)
        if len(found) >= limit:
            break
    for m in _BARE_ATS.finditer(text):
        found.append("https://" + m.group(0))
    return found


def detect_html(text: str, page_url: str = "") -> list[Hit]:
    """Every hiring-system board a page names, in the order it names them."""
    hits = []
    for url in urls_in(text):
        lowered = url.lower()
        # Cheap pre-filter: nearly every URL on a page is the site's own.
        if not any(k in lowered for k in _HOST_HINTS):
            continue
        hit = detect_url(url)
        if hit:
            hits.append(hit)
    return hits + named_in_markup(text, page_url)


# Embed snippets that name the board in code rather than in a URL: Webflow's
# Ashby snippet (`const JOB_BOARD_NAME = "synthesia"`), WordPress Greenhouse
# modules (`data-board="legion"`), Lever's jobs widget, BambooHR's embed. Each
# is only read on a page that also names the system, and is turned into the
# board's ordinary URL so it gets the same slug checks as any other.
_NAMED_IN_MARKUP: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"JOB_BOARD_NAME\s*=\s*[\"']([\w.%-]+)[\"']"), "ashby", "https://jobs.ashbyhq.com/{}"),
    (re.compile(r"class=[\"'][^\"']*greenhouse[^\"']*[\"'][^<>]{0,200}?\bdata-board(?:[-_]?token)?=[\"']([\w-]+)[\"']",
                re.I), "greenhouse", "https://job-boards.greenhouse.io/{}"),
    (re.compile(r"\b(?:board_token|boardToken)[\"']?\s*[:=]\s*[\"']([\w-]+)[\"']"), "greenhouse",
     "https://job-boards.greenhouse.io/{}"),
    (re.compile(r"leverJobsOptions\s*=\s*\{[^}]{0,300}?accountName\s*:\s*[\"']([\w.-]+)[\"']"), "lever",
     "https://jobs.lever.co/{}"),
    (re.compile(r"data-bamboohr-domain=[\"']([\w-]+)[\"']", re.I), "bamboohr", "https://{}.bamboohr.com/careers"),
]


def named_in_markup(text: str, page_url: str = "") -> list[Hit]:
    out: list[Hit] = []
    lowered = None
    for pattern, word, canonical in _NAMED_IN_MARKUP:
        if lowered is None:
            lowered = (text or "").lower()
        if word not in lowered:
            continue
        for m in pattern.finditer(text or ""):
            hit = detect_url(canonical.format(m.group(1)))
            if hit and hit.slug:
                out.append(Hit(hit.system, hit.slug, hit.board_url, clean_url(page_url) or hit.seen_at))
    return out


def fingerprints(text: str) -> list[str]:
    """Hiring systems whose traces a page carries without naming a board."""
    return [system for system, pattern in _FINGERPRINTS if pattern.search(text or "")]


_HOST_HINTS = tuple(sorted({
    "greenhouse", "grnh.se", "lever.co", "ashbyhq", "workable", "applytojob", "jazz.co", "bamboohr",
    "breezy", "recruitee", "teamtailor", "smartrecruiters", "jobvite", "icims", "adp.com",
    "ultipro", "ukg.net", "paycomonline", "paylocity", "dayforcehcm", "taleo", "successfactors",
    "jobs2web", "personio", "rippling", "oraclecloud", "pinpointhq", "avature", "eightfold",
    "phenompeople", "governmentjobs", "schooljobs", "peopleadmin", "applicantpro", "hiringthing",
    "careerplug", "zohorecruit", "freshteam", "comeet", "homerun", "jobs.gem.com", "isolvedhire",
    "recruitingbypaycor", "csod.com", "brassring", "hrmdirect", "applicantstack", "trakstar",
    "recruiterbox", "jobscore", "catsone", "polymer.co", "join.com", "welcometothejungle",
    "talentlyft", "careers-page.com", "recruiterflow", "jobdiva", "trinethire", "hireology",
    "hirebridge", "theapplicantmanager", "silkroad", "applitrack", "jobs.gusto", "factorialhr",
    "clearcompany", "crelate", "keka.com", "darwinbox", "wellfound", "angel.co",
    "workatastartup", "usajobs", "myworkday",
}))


# ---------------------------------------------------------------------------
# Company names
# ---------------------------------------------------------------------------

# Legal forms, stripped from the end of a name for matching. "Group" and
# "Holdings" stay: stripping them merges distinct companies that share a word.
LEGAL_WORDS = frozenset("""
inc incorporated llc llp lp ltd limited corp corporation co company plc pllc pc gmbh ag sa bv
nv pty pte srl spa sarl oy ab as kk
""".split())


def normalize_name(name: str | None) -> str:
    """'Zagg, Inc.' -> 'zagg inc'; 'L.L.C.' letters joined: 'acme llc'."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    merged: list[str] = []
    run = ""
    for word in words:                      # "l l c" -> "llc", "i b m" -> "ibm"
        if len(word) == 1:
            run += word
            continue
        if run:
            merged.append(run)
            run = ""
        merged.append(word)
    if run:
        merged.append(run)
    return " ".join(merged)


def name_key(name: str | None) -> str:
    """The name with its legal form and a leading "the" removed: the match key."""
    words = normalize_name(name).split()
    while len(words) > 1 and words[-1] in LEGAL_WORDS:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def compact(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


_GENERIC_WORDS = frozenset("""
and of the for group holdings services solutions systems technologies technology consulting
international global partners associates enterprises industries management america american
usa us national health healthcare care medical financial capital bank labs software digital
""".split())

# Words a board name often adds to a company's: "acmecareers", "acme-hq".
_SLUG_NOISE = re.compile(r"(careers?|jobs|hiring|talent|recruiting|external|global|group|"
                         r"inc|llc|corp|hq|usa|us|team|hr|portal|official)$")


def slug_matches_name(slug: str | None, name: str | None, domain: str | None = None) -> bool:
    """Does a board name plausibly belong to this company?

    Used where a page can carry other companies' boards -- an aggregator's job
    page with ads in its sidebar, a venture fund's portfolio jobs page -- so a
    board is only taken when its name resembles the company's.
    """
    if not slug:
        return False
    head = re.split(r"[:/]", slug)[0]
    s = compact(head)
    if not s:
        return False
    keys = set()
    words = name_key(name).split()
    if words:
        keys.add(compact(" ".join(words)))
        keys.add(compact(normalize_name(name)))
        distinctive = [w for w in words if w not in _GENERIC_WORDS]
        if distinctive and len(distinctive[0]) >= 4:
            keys.add(distinctive[0])
        if len(words) >= 2:
            keys.add("".join(w[0] for w in words if w))
    root = compact(domain_root(domain)) if domain else ""
    if root:
        keys.add(root)
    keys.discard("")
    trimmed = s
    for _ in range(2):
        trimmed = _SLUG_NOISE.sub("", trimmed) or trimmed
    for candidate in {s, trimmed}:
        if candidate in keys:
            return True
        for key in keys:
            if len(key) >= 4 and len(candidate) >= 4 and (candidate.startswith(key) or key.startswith(candidate)):
                return True
    return False


@dataclass(frozen=True)
class DomainCandidate:
    domain: str
    country: str = ""
    employees: int = 0


def pick_domain(name: str, candidates: list[DomainCandidate]) -> tuple[str | None, str]:
    """Choose the company's domain among same-named companies, or decline.

    Returns (domain, reason). One candidate is taken. Among several, a domain
    that spells the name ("hinge.co" for Hinge) wins outright; otherwise the
    biggest is taken only when it dwarfs the next (Cisco, 139,423 employees,
    against 55) -- a near tie ("Sierra": 42 against 38) is left undecided.
    """
    best: dict[str, DomainCandidate] = {}
    for cand in candidates:
        domain = clean_domain(cand.domain)
        if not domain:
            continue
        if domain not in best or cand.employees > best[domain].employees:
            best[domain] = DomainCandidate(domain, cand.country, cand.employees)
    if not best:
        return None, "no domain in the lists"
    if len(best) == 1:
        return next(iter(best)), "only match"

    key = compact(name_key(name))
    spelled = [d for d in best if key and compact(domain_root(d)) == key]
    if len(spelled) == 1:
        return spelled[0], "domain spells the name"

    def rank(c: DomainCandidate):
        return (compact(domain_root(c.domain)) == key,
                (c.country or "").lower() in ("united states", "us"), c.employees)

    ordered = sorted(best.values(), key=rank, reverse=True)
    top, runner = ordered[0], ordered[1]
    if top.employees >= 20 and top.employees >= 5 * max(runner.employees, 1):
        return top.domain, f"largest of {len(best)} ({top.employees:,} vs {runner.employees:,} staff)"
    shown = ", ".join(c.domain for c in ordered[:3])
    return None, f"ambiguous: {len(best)} companies share the name ({shown})"


# ---------------------------------------------------------------------------
# Links and pages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    url: str
    text: str
    kind: str = "a"         # a | iframe | refresh


_A_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.S | re.I)
_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_LABEL = re.compile(r"""\b(?:aria-label|title)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ALT = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_FRAME = re.compile(r"""<i?frame\b[^>]*?\b(?:data-)?src\s*=\s*["']([^"']+)["']""", re.I)
_BASE = re.compile(r"""<base\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.S | re.I)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.S | re.I)
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.S | re.I)


def _text(fragment: str) -> str:
    return _WS.sub(" ", _html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _attr(pattern: re.Pattern, attrs: str) -> str:
    m = pattern.search(attrs)
    return _html.unescape(next((g for g in m.groups() if g is not None), "")) if m else ""


def extract_links(page: str, base_url: str) -> list[Link]:
    """Anchors (with their visible text, or label/alt when they have none) and frames."""
    page = page or ""
    base = base_url
    m = _BASE.search(page[:20000])
    if m:
        base = urljoin(base_url, _html.unescape(m.group(1).strip()))
    out: list[Link] = []
    for m in _A_TAG.finditer(page):
        attrs, inner = m.group(1), m.group(2)
        href = _attr(_HREF, attrs).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "sms:")):
            continue
        text = _text(inner) or _attr(_LABEL, attrs) or _attr(_ALT, inner)
        try:
            url = urljoin(base, href).split("#")[0]
        except ValueError:
            continue
        if url.startswith(("http://", "https://")):
            out.append(Link(url, text[:200], "a"))
    for m in _FRAME.finditer(page):
        try:
            url = urljoin(base, _html.unescape(m.group(1).strip())).split("#")[0]
        except ValueError:
            continue
        if url.startswith(("http://", "https://")):
            out.append(Link(url, "", "iframe"))
    return out


def page_title(page: str) -> str:
    m = _TITLE.search(page or "")
    return _text(m.group(1))[:300] if m else ""


def page_heading(page: str) -> str:
    m = _H1.search(page or "")
    return _text(m.group(1))[:300] if m else ""


def visible_text(page: str, limit: int = 200_000) -> str:
    return _text(_SCRIPT_STYLE.sub(" ", (page or "")[:limit]))


_REFRESH = re.compile(
    r"""<meta\b[^>]*http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']?\s*\d*\s*;?\s*url\s*=\s*['"]?([^"'>\s]+)"""
    r"""|<meta\b[^>]*content\s*=\s*["']?\s*\d*\s*;\s*url\s*=\s*['"]?([^"'>\s]+)[^>]*http-equiv\s*=\s*["']?refresh""",
    re.I)
_JS_REDIRECT = re.compile(
    r"""(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["']([^"']+)["']""")


def meta_refresh(page: str, base_url: str) -> str | None:
    """The target of a <meta http-equiv="refresh"> redirect, if the page has one."""
    m = _REFRESH.search((page or "")[:20000])
    if not m:
        return None
    target = _html.unescape(m.group(1) or m.group(2) or "").strip()
    return urljoin(base_url, target) if target else None


def script_redirect(page: str, base_url: str) -> str | None:
    """A bare `location = "..."` page: only read on tiny pages, where it is the whole point."""
    if len(page or "") > 6000:
        return None
    m = _JS_REDIRECT.search(page or "")
    if not m:
        return None
    target = m.group(1).strip()
    if not target or target.startswith(("#", "javascript:")):
        return None
    return urljoin(base_url, target)


def hiring_org_urls(page: str) -> list[str]:
    """Company URLs a job page states in its schema.org JobPosting data."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            org = node.get("hiringOrganization")
            if isinstance(org, dict):
                for key in ("url", "sameAs"):
                    value = org.get(key)
                    for item in (value if isinstance(value, list) else [value]):
                        if isinstance(item, str) and item.startswith("http"):
                            out.append(item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script\s*>", page or "", re.S | re.I):
        try:
            walk(json.loads(m.group(1).strip()))
        except ValueError:
            continue
    return out


# --- what careers pages say ---------------------------------------------------

_CAREER_WORD = (r"careers?|jobs?|hiring|vacanc(?:y|ies)|employment|openings?|open\s+positions|"
                r"open\s+roles|join\s+(?:us|our\s+team|the\s+team)|work\s+(?:with|for)\s+us|"
                r"karriere|stellenangebote|stellen|carri[eè]res?|emplois?|recrutement|empleos?|"
                r"trabaja\s+con\s+nosotros|vacatures|werken\s+bij|lavora\s+con\s+noi|carreiras?|"
                r"trabalhe\s+conosco|vagas|karri[aä]r|ty[oö]paikat|rekrytointi|kariera|kariyer")
CAREER_WORD = re.compile(rf"\b(?:{_CAREER_WORD})\b", re.I)

_STRONG_TEXT = re.compile(
    r"^(?:careers?|jobs|join us|join our team|join the team|work with us|work for us|"
    r"we(?: a)?re hiring|now hiring|hiring|open positions|open roles|job openings|"
    r"current openings|job opportunities|career opportunities|employment|"
    r"employment opportunities|vacancies|current vacancies|careers page|see open roles|"
    r"view open roles|view open positions|view (?:all )?jobs|see (?:all )?jobs|explore careers|"
    r"join|work here|jobs (?:and|&) careers|careers (?:and|&) jobs|"
    r"(?:careers?|jobs) (?:at|@) .{2,40}|work (?:at|for|with) .{2,40}|join .{2,30} team|"
    r"life at .{2,40}|working at .{2,40}|"
    r"karriere|jobs (?:und|&) karriere|stellenangebote|carri[eè]res?|emplois|recrutement|"
    r"empleo|trabaja con nosotros|vacatures|werken bij.{0,30}|lavora con noi|carreiras|"
    r"trabalhe conosco|jobb|karri[aä]r)$", re.I)

_BAD_TEXT = re.compile(
    r"career (?:advice|tips|guide|development|center|centre|services|coach|path|change|fair)|"
    r"job (?:aid|seekers?|description template|board software|search tips)|steve jobs|"
    r"jobs to be done|find (?:a |your )?(?:job|talent|candidates)|post (?:a )?job|"
    r"hire (?:talent|developers|engineers|staff)|for (?:employers|recruiters|hiring managers)|"
    r"employer|log ?in|sign ?(?:in|up)|candidate (?:login|portal)|student|alumni|course|"
    r"training|certif|webinar|press|news|blog|podcast|case stud|customer stor|privacy|cookie|"
    r"terms|staffing services", re.I)

_PATH_CAREER = re.compile(
    r"^(?:careers?|jobs?|join-?us|join|work-?with-?us|work-?for-?us|hiring|vacancies|"
    r"karriere|jobs-?karriere|vacatures|carrieres?|carri%c3%a8res?|empleo|employment|"
    r"open-?positions|open-?roles|openings|stellenangebote|werken-?bij|lavora-?con-?noi|"
    r"trabalhe-?conosco|(?:careers?|jobs)-.+|.+-(?:careers?|jobs)|life-?at-?.*|work-?at-?.*)$",
    re.I)
_PATH_BAD = frozenset("""
blog blogs news press article articles stories story resources insights podcast podcasts events
event webinar webinars docs help support login signin account cart shop products product
customers case-studies solutions category tag tags learn academy guides
""".split())
_HOST_CAREER = re.compile(r"^(?:careers?|jobs?|join|work|talent|apply|hiring|recruiting|"
                          r"karriere|jobs-[\w-]+|careers-[\w-]+|werkenbij|vacatures)\.", re.I)

# Places a careers link must never lead: social networks and job boards that
# list the company rather than being its own careers site.
_OFFSITE_NEVER = frozenset("""
linkedin.com facebook.com twitter.com x.com instagram.com youtube.com tiktok.com pinterest.com
glassdoor.com indeed.com ziprecruiter.com monster.com dice.com careerbuilder.com simplyhired.com
builtin.com google.com apple.com github.com medium.com wikipedia.org crunchbase.com
""".split())
_FILE_EXT = re.compile(r"\.(?:pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.I)


@dataclass(frozen=True)
class Candidate:
    url: str
    score: int
    text: str
    hit: Hit | None = None


def score_link(link: Link, site_domain: str | None) -> int:
    """How likely a homepage link is the company's careers page. 40+ is a candidate."""
    url = link.url
    try:
        parts = urlparse(url)
    except ValueError:
        return -100
    if parts.scheme not in ("http", "https"):
        return -100
    host = (parts.hostname or "").lower()
    reg = registrable_domain(host)
    if detect_url(url):
        hit = detect_url(url)
        return 100 + (10 if hit and hit.slug else 0) + (5 if CAREER_WORD.search(link.text or "") else 0)
    if reg in _OFFSITE_NEVER:
        return -100
    if _FILE_EXT.search(parts.path or ""):
        return -50

    text = _WS.sub(" ", re.sub(r"[^\w&@' ]+", " ", (link.text or "").lower())).strip()
    text = text.replace("we're", "were").replace("'", "")
    score = 0
    if text and _STRONG_TEXT.match(text):
        score += 50
    elif text and len(text) <= 40 and CAREER_WORD.search(text):
        score += 30
    elif text and len(text) <= 80 and CAREER_WORD.search(text):
        score += 10
    if text and _BAD_TEXT.search(text):
        score -= 40

    segments = [s for s in unquote(parts.path or "").lower().split("/") if s]
    for index, segment in enumerate(segments[:4]):
        if _PATH_CAREER.match(segment):
            before = set(segments[:index])
            if before & _PATH_BAD:
                score -= 20
            else:
                score += 30 if index == 0 else 20
            break
    if any(s in _PATH_BAD for s in segments[:1]) and score < 50:
        score -= 10

    same_site = bool(site_domain) and reg == site_domain
    if _HOST_CAREER.match(host) and (same_site or not site_domain):
        score += 35
    if site_domain and not same_site:
        score -= 15
    if len(segments) >= 4:
        score -= 5
    return score


def careers_root(url: str) -> str:
    """'/careers/engineering/123' -> '/careers': the page above a careers link's detail."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    segments = [s for s in (parts.path or "").split("/") if s]
    for index, segment in enumerate(segments[:4]):
        if _PATH_CAREER.match(unquote(segment).lower()):
            path = "/" + "/".join(segments[: index + 1])
            return f"{parts.scheme}://{parts.netloc}{path}"
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"


def rank_careers_links(links: list[Link], site_domain: str | None, minimum: int = 40) -> list[Candidate]:
    """Homepage links worth following, best first, one per careers page."""
    best: dict[str, Candidate] = {}
    for link in links:
        score = score_link(link, site_domain)
        if score < minimum:
            continue
        hit = detect_url(link.url)
        key = link.url if hit else careers_root(link.url)
        url = link.url if hit else (key if score < 100 else link.url)
        current = best.get(key)
        if current is None or score > current.score:
            best[key] = Candidate(url, score, link.text, hit)
    return sorted(best.values(), key=lambda c: (-c.score, len(c.url)))


_SOFT_404 = re.compile(r"\b404\b|not found|page (?:cannot|can'?t|could not) be found|"
                       r"page doesn'?t exist|no longer (?:exists|available)|error page|"
                       r"seite nicht gefunden|page introuvable", re.I)
_LISTING_PHRASES = re.compile(
    r"open positions|open roles|job openings|current openings|join our team|we'?re hiring|"
    r"view (?:all )?jobs|apply now|equal opportunity employer|life at|our culture|"
    r"no (?:current )?(?:open positions|openings|vacancies)|search jobs|all jobs|job search|"
    r"vacancies|career opportunities|explore (?:our )?(?:open )?roles", re.I)


def is_soft_404(page: str) -> bool:
    head = f"{page_title(page)} | {page_heading(page)}"
    return bool(_SOFT_404.search(head))


def careers_signal(page: str, url: str) -> int:
    """How much a fetched page reads like a careers page. 3+ is convincing."""
    score = 0
    if CAREER_WORD.search(page_title(page)):
        score += 2
    if CAREER_WORD.search(page_heading(page)):
        score += 2
    try:
        parts = urlparse(url)
    except ValueError:
        parts = None
    if parts is not None:
        segments = [s for s in unquote(parts.path or "").lower().split("/") if s]
        if any(_PATH_CAREER.match(s) for s in segments[:3]) or _HOST_CAREER.match(parts.hostname or ""):
            score += 2
    phrases = {m.group(0).lower() for m in _LISTING_PHRASES.finditer(visible_text(page, 300_000))}
    score += min(len(phrases), 3)
    if detect_html(page) or fingerprints(page):
        score += 3
    return score


_BOT_WALL = re.compile(r"just a moment|attention required|cf-chl|challenge-platform|"
                       r"checking your browser|ddos protection|captcha|are you a robot|"
                       r"access denied|request unsuccessful|incapsula|px-captcha|datadome|"
                       r"sucuri website firewall|bot protection|enable javascript and cookies",
                       re.I)


def bot_wall(status: int, headers: dict | None, page: str) -> str | None:
    """Name the gate when a site answers with a challenge page instead of itself."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    server = (headers.get("server") or "").lower()
    head = (page or "")[:30000]
    if status in (403, 429, 503) or (status == 200 and len(head) < 20000):
        if "cf-ray" in headers or "cloudflare" in server:
            if status in (403, 503) or re.search(r"cf-chl|challenge-platform|just a moment", head, re.I):
                return "cloudflare"
        if status in (403, 429, 503) and _BOT_WALL.search(head):
            if "akamai" in server or "reference #" in head.lower():
                return "akamai"
            if "incapsula" in head.lower():
                return "incapsula"
            if "datadome" in head.lower() or "datadome" in str(headers).lower():
                return "datadome"
            return "bot-wall"
        if status == 200 and re.search(r"cf-chl|challenge-platform|px-captcha|captcha-delivery", head, re.I):
            return "bot-wall"
    if status == 403:
        return "forbidden"
    return None


_PARKED = re.compile(r"this domain (?:name )?(?:is|may be) for sale|buy this domain|"
                     r"domain (?:is )?parked|parked free|domain parking|"
                     r"the domain .{1,60} (?:is|may be) for sale|hugedomains|sedoparking|"
                     r"afternic|dan\.com|bodis\.com|parkingcrew|domainmarket|"
                     r"undeveloped\.com|is available for purchase", re.I)
_PARKED_HOSTS = ("hugedomains.com", "sedo.com", "dan.com", "afternic.com", "godaddy.com",
                 "parkingcrew.net", "bodis.com", "above.com", "undeveloped.com",
                 "domainmarket.com", "buydomains.com", "sedoparking.com", "parklogic.com")


def is_parked(page: str, final_url: str) -> bool:
    host = host_of(final_url)
    if any(host == h or host.endswith("." + h) for h in _PARKED_HOSTS):
        return True
    return bool(_PARKED.search((page or "")[:60000])) and len(visible_text(page, 60000)) < 3000


# --- choosing a board --------------------------------------------------------

def choose_board(hits: list[Hit], name: str | None = None, domain: str | None = None,
                 strict: bool = False) -> Hit | None:
    """The one board a page belongs to, or None when it cannot say.

    A careers page names its own board, usually many times. A page naming
    several different boards is something else -- a fund's portfolio jobs page,
    a job board, a page full of ads -- and there only a board whose name
    resembles the company's is taken. `strict` applies that rule always: for
    pages that are not the company's own.
    """
    if not hits:
        return None
    counts: Counter = Counter()
    first: dict[tuple, Hit] = {}
    for hit in hits:
        key = (hit.system, (hit.slug or "").lower())
        counts[key] += 1
        first.setdefault(key, hit)
    with_slug = [k for k in first if k[1]]
    matching = [k for k in with_slug if slug_matches_name(first[k].slug, name, domain)]
    if matching:
        return first[max(matching, key=lambda k: counts[k])]
    if strict:
        return None
    if len(with_slug) == 1:
        return first[with_slug[0]]
    if len(with_slug) == 2:
        a, b = sorted(with_slug, key=lambda k: -counts[k])
        if counts[a] > counts[b]:
            return first[a]
        return first[with_slug[0]] if a[0] == b[0] and counts[a] >= 2 else None
    if len(with_slug) > 2:
        return None
    # Only boards without a name (a short link, a vendor page): the system is still known.
    systems = {k[0] for k in first}
    return first[next(iter(first))] if len(systems) == 1 else None


def careers_url_from_posting(url: str) -> str | None:
    """A job on the company's own site -> the careers page above it, if the URL shows one.

    'https://www.coinbase.com/careers/positions/8177619?gh_jid=8177619' ->
    'https://www.coinbase.com/careers'; 'https://join.jfrog.com/job/?job=1' ->
    'https://join.jfrog.com/'.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        return None
    if _HOST_CAREER.match(host):
        return f"{parts.scheme}://{parts.netloc}/"
    # Only a segment that names a listing ("careers", "jobs", "open-positions"):
    # "/job/devops-engineer" names one posting, and "/job" is rarely a page.
    segments = [s for s in (parts.path or "").split("/") if s]
    for index, segment in enumerate(segments[:4]):
        if _INDEX_SEGMENT.match(unquote(segment).lower()):
            return f"{parts.scheme}://{parts.netloc}/" + "/".join(segments[: index + 1])
    return None


_INDEX_SEGMENT = re.compile(
    r"^(?:careers?|jobs|join-?us|join|work-?with-?us|vacancies|karriere|vacatures|carrieres?|"
    r"open-?positions|open-?roles|openings|employment|opportunities|stellenangebote|"
    r"(?:careers?|jobs)-.+|.+-(?:careers|jobs))$")


# Job boards, aggregators and click trackers: a page on one of these is about a
# company, never the company's own site, so it is followed but never taken as
# the employer's domain.
AGGREGATOR_DOMAINS = frozenset("""
adzuna.com adzuna.co.uk adzuna.ca adzuna.com.au jooble.org jobviewtrack.com careerjet.com
glassdoor.com ziprecruiter.com monster.com dice.com indeed.com findwork.dev himalayas.app
jobicy.com ycombinator.com usajobs.gov remoteok.com remoteok.io themuse.com weworkremotely.com
workingnomads.com linkedin.com simplyhired.com talent.com appcast.io recruitics.com lensa.com
careerbuilder.com jobget.com jobs2careers.com neuvoo.com snagajob.com builtin.com wellfound.com
angel.co otta.com welcometothejungle.com google.com bing.com facebook.com twitter.com x.com
""".split())
