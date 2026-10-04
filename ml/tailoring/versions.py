"""Check tool versions on a resume against the dates of the work they are listed under.

A version released after a job ended cannot have been used in that job, so it
is flagged. Every check says where its release date came from; a version the
sources do not cover is Unknown -- never guessed.

Release dates come from endoflife.date (https://endoflife.date, one public
API call per product, cached in data/tailoring/releases/ for 30 days), and
from ml/tailoring/release_dates.json for anything added by hand, which wins.

Statuses:  confirmed · mismatch · unknown · possible (a tool named with no
version: the versions out during that work, for the report only).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

CACHE = PROJECT_ROOT / "data" / "tailoring" / "releases"
LOCAL = Path(__file__).with_name("release_dates.json")
API = "https://endoflife.date/api/{}.json"
MAX_AGE = 30 * 86400

# How the resume writes it -> the endoflife.date product.
PRODUCTS = {
    "kubernetes": "kubernetes", "terraform": "terraform", "python": "python", "java": "oracle-jdk",
    "spring boot": "spring-boot", "angular": "angular", "react": "react", "node.js": "nodejs",
    "nodejs": "nodejs", "jenkins": "jenkins", "ansible": "ansible-core", "docker": "docker-engine",
    "postgresql": "postgresql", "postgres": "postgresql", "kafka": "apache-kafka", "redis": "redis",
    "argo cd": "argo-cd", "argocd": "argo-cd", "django": "django", "go": "go", "golang": "go",
    "mongodb": "mongodb", "mysql": "mysql", "elasticsearch": "elasticsearch", ".net": "dotnet",
    "php": "php", "ruby": "ruby", "rails": "rails", "vue": "vue", "grafana": "grafana",
    "prometheus": "prometheus", "openshift": "red-hat-openshift", "gitlab": "gitlab", "nginx": "nginx",
    "istio": "istio", "ubuntu": "ubuntu", "rhel": "rhel", "typescript": "typescript",
}
_NAMES = sorted(PRODUCTS, key=len, reverse=True)
_VERSION = r"v?(\d+(?:\.\d+){0,2}(?:\.x)?\+?(?:\s*/\s*v?\d+(?:\.\d+)?(?:\.x)?)*)"
FOUND = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in _NAMES) + r")\s*\(?\s*" + _VERSION
                   + r"(?![\w.])", re.I)


@dataclass
class VersionCheck:
    tool: str
    version: str
    status: str
    where: str
    fact_id: str
    release_date: str | None
    source: str
    detail: str


def _fetch(product: str) -> tuple[list[dict], str] | None:
    """(cycles, source) for a product, from the hand file, the cache, or the API."""
    try:
        local = json.loads(LOCAL.read_text()).get(product)
        if local:
            return local, f"{LOCAL.name} (entered by hand)"
    except (OSError, ValueError):
        pass
    cached = CACHE / f"{product}.json"
    try:
        data = json.loads(cached.read_text())
        if time.time() - data["fetched"] < MAX_AGE:
            return data["cycles"], data["source"]
    except (OSError, ValueError, KeyError):
        data = None
    try:
        import requests
        resp = requests.get(API.format(product), timeout=15,
                            headers={"User-Agent": "AI-Job-Applier/1.0 (personal resume tool)"})
        if resp.status_code != 200:
            return (data["cycles"], data["source"]) if data else None
        cycles = resp.json()
        source = f"endoflife.date/{product} (fetched {time.strftime('%Y-%m-%d')})"
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"fetched": time.time(), "source": source, "cycles": cycles}))
        return cycles, source
    except Exception:
        return (data["cycles"], data["source"]) if data else None


def release_of(cycles: list[dict], version: str) -> str | None:
    """The release date of `version`, or None when the source cannot say.

    "1.27" and "17" are looked up as written (or "1.27.3" by its 1.27 cycle).
    "2.x", "2" and "2+" mean the major version's first release, which is only
    known if the source lists that first release -- "2" or "2.0". The source
    listing Ansible from 2.9 does not say when 2.0 came out, and taking its
    oldest 2.* entry flagged three-year-old work as impossible.
    """
    v = version.strip().lower().lstrip("v").rstrip("+")
    major_only = v.endswith(".x") or "." not in v
    v = re.sub(r"\.x$", "", v)
    by_cycle = {str(c.get("cycle")).lower(): c.get("releaseDate") for c in cycles}
    if major_only:
        for key in (v, v + ".0"):
            if by_cycle.get(key):
                return by_cycle[key]
        return None
    for key in (v, ".".join(v.split(".")[:2])):
        if by_cycle.get(key):
            return by_cycle[key]
    return None


def _month(date: str | None) -> str | None:
    return date[:7] if date else None


def check(model, jd_text: str = "", fetch=_fetch) -> list[VersionCheck]:
    from ml.tailoring.structure import today
    from ml.tailoring.synonyms import mentions
    checks: list[VersionCheck] = []
    cache: dict[str, tuple[list[dict], str] | None] = {}

    def product_data(tool):
        product = PRODUCTS[tool.lower()]
        if product not in cache:
            cache[product] = fetch(product)
        return cache[product]

    seen: set[tuple[str, str, str]] = set()
    for fact in model.facts:
        entry = model.entry(fact.entry) if fact.entry else None
        for m in FOUND.finditer(fact.text):
            tool, raw = m.group(1), m.group(2)
            for version in re.split(r"\s*/\s*", raw):
                key = (tool.lower(), version.lower(), entry.id if entry else fact.section)
                if key in seen:
                    continue                    # the same version named twice in one job
                seen.add(key)
                where = (f"{entry.title} at {entry.employer} ({entry.dates})" if entry
                         else fact.section.capitalize())
                data = product_data(tool)
                released = release_of(data[0], version) if data else None
                source = data[1] if data else "no release data available"
                if not released:
                    checks.append(VersionCheck(tool, version, "unknown", where, fact.id, None, source,
                                               f"The release date of {tool} {version} could not be "
                                               f"verified, so this was not checked."))
                    continue
                if not entry or not entry.start:
                    checks.append(VersionCheck(tool, version, "confirmed", where, fact.id, released, source,
                                               f"{tool} {version} was released {released}; it is not "
                                               f"tied to dated work, so only the release was checked."))
                    continue
                ended = today() if entry.present else (entry.end or today())
                if _month(released) > ended:
                    checks.append(VersionCheck(tool, version, "mismatch", where, fact.id, released, source,
                                               f"{tool} {version} was released {released}, after this "
                                               f"work ended ({ended}). It could not have been used there."))
                else:
                    checks.append(VersionCheck(tool, version, "confirmed", where, fact.id, released, source,
                                               f"{tool} {version} was released {released}, before this "
                                               f"work ended ({ended})."))

    # Tools the posting names, used in a job without a version: what was out then.
    asked_names = [n for n in _NAMES if mentions(jd_text, n)] if jd_text else []
    # One name per product: "postgres" and "postgresql" are the same check.
    asked = {PRODUCTS[n]: n for n in reversed(asked_names)}.values()
    versioned = {(c.tool.lower(), c.fact_id) for c in checks}
    for entry in model.entries:
        if entry.section != "experience" or not entry.start:
            continue
        ended = today() if entry.present else (entry.end or today())
        for tool in sorted(asked):
            facts = [model.fact(f) for f in entry.facts if mentions(model.fact(f).text, tool)]
            if not facts or any((tool, f.id) in versioned for f in facts):
                continue
            data = product_data(tool)
            if not data:
                continue
            out = [str(c["cycle"]) for c in data[0]
                   if c.get("releaseDate") and _month(c["releaseDate"]) <= ended
                   and (c.get("eol") in (False, None, True) or str(c.get("eol"))[:7] >= entry.start)]
            if out:
                checks.append(VersionCheck(tool, ", ".join(out[:6]), "possible",
                                           f"{entry.title} at {entry.employer} ({entry.dates})",
                                           facts[0].id, None, data[1],
                                           f"Your resume names {tool} here without a version. Versions out "
                                           f"during this work: {', '.join(out[:6])}. Shown for reference "
                                           f"only; nothing is added to your resume."))
    return checks


def as_dicts(checks: list[VersionCheck]) -> list[dict]:
    return [asdict(c) for c in checks]
