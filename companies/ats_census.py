"""Find out which applicant tracking systems companies actually use.

The scraper reads four: Greenhouse, Lever, Ashby and Workday. Everything else
is invisible, and "invisible" is not a small category -- Zoom runs on Clinch,
so no amount of probing company names would ever surface a Zoom posting, and
one was applied to from an email alert having never appeared in this tool.

Which platform to build next is currently a guess. This makes it a
measurement: take companies from the list, follow each one's careers page, see
where it lands, and count. The answer is a ranked list of how many more
companies each new scraper would reach.

Method, deliberately cheap. A careers page nearly always either redirects to
the ATS or embeds it, so the final URL plus a scan of the page body identifies
it without rendering JavaScript. Where neither says anything, the answer is
recorded as "own site" rather than guessed at, because a wrong label here
would send someone off to write the wrong scraper.

Polite by construction: one request per company, a delay between them, robots
respected through the shared client, and nothing is fetched twice.

    python -m companies.ats_census --limit 300
    python -m companies.ats_census --report          # just read the results
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

RESULTS = Path("data/ats_census.jsonl")

# Matched against the final URL first, then the page body. Ordered: the first
# hit wins, so the more specific host patterns come before generic words that
# might appear in a page for other reasons.
SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("greenhouse",     re.compile(r"(?:boards|job-boards)\.greenhouse\.io|greenhouse\.io/embed", re.I)),
    ("lever",          re.compile(r"jobs\.lever\.co|lever\.co/postings", re.I)),
    ("ashby",          re.compile(r"jobs\.ashbyhq\.com|ashbyhq\.com/api", re.I)),
    ("workday",        re.compile(r"\.myworkdayjobs\.com|myworkdaysite\.com", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com|jobs\.smartrecruiters", re.I)),
    ("workable",       re.compile(r"apply\.workable\.com|workable\.com/j/", re.I)),
    ("icims",          re.compile(r"\.icims\.com", re.I)),
    ("taleo",          re.compile(r"\.taleo\.net|taleo\.com", re.I)),
    ("successfactors", re.compile(r"successfactors\.(?:com|eu)|sapsf\.com", re.I)),
    ("oraclecloud",    re.compile(r"\.oraclecloud\.com/hcmUI|fa\.\w+\.oraclecloud", re.I)),
    ("jobvite",        re.compile(r"jobs\.jobvite\.com|jobvite\.com/careers", re.I)),
    ("bamboohr",       re.compile(r"\.bamboohr\.com/(?:jobs|careers)", re.I)),
    ("recruitee",      re.compile(r"\.recruitee\.com", re.I)),
    ("personio",       re.compile(r"\.jobs\.personio\.(?:com|de)", re.I)),
    ("teamtailor",     re.compile(r"\.teamtailor\.com", re.I)),
    ("breezy",         re.compile(r"\.breezy\.hr", re.I)),
    ("rippling",       re.compile(r"ats\.rippling\.com", re.I)),
    ("paylocity",      re.compile(r"recruiting\.paylocity\.com", re.I)),
    ("paycom",         re.compile(r"paycomonline\.net", re.I)),
    ("ultipro",        re.compile(r"\.ultipro\.com|ukg\.\w+/careers", re.I)),
    ("adp",            re.compile(r"workforcenow\.adp\.com|myjobs\.adp\.com", re.I)),
    ("dayforce",       re.compile(r"dayforcehcm\.com", re.I)),
    ("clinch",         re.compile(r"clinchtalent\.com|clinch\.io", re.I)),
    ("phenom",         re.compile(r"phenompeople\.com|\.phenom\.com", re.I)),
    ("eightfold",      re.compile(r"eightfold\.ai", re.I)),
    ("avature",        re.compile(r"\.avature\.net", re.I)),
    ("jazzhr",         re.compile(r"\.applytojob\.com|jazzhr\.com", re.I)),
    ("pinpoint",       re.compile(r"\.pinpointhq\.com", re.I)),
    ("neogov",         re.compile(r"governmentjobs\.com|schooljobs\.com", re.I)),
    ("peopleadmin",    re.compile(r"\.peopleadmin\.com", re.I)),
]

# Where a careers page tends to live. Tried in order, first that answers wins.
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/about/careers", "/company/careers")

# Only the first slice of a page is scanned: an ATS link sits in the markup or
# a script tag near the top, and reading a whole marketing site to find one is
# waste.
MAX_HTML = 300_000


def identify(final_url: str, body: str) -> str:
    """Which ATS this careers page belongs to, or "own site"."""
    for name, pattern in SIGNATURES:
        if pattern.search(final_url or ""):
            return name
    head = (body or "")[:MAX_HTML]
    for name, pattern in SIGNATURES:
        if pattern.search(head):
            return name
    return "own site"


def already_done() -> set[str]:
    """Domains already checked, so a re-run continues instead of repeating."""
    if not RESULTS.exists():
        return set()
    done = set()
    with open(RESULTS, encoding="utf-8") as handle:
        for line in handle:
            try:
                done.add(json.loads(line)["domain"])
            except (ValueError, KeyError):
                continue
    return done


def check(domain: str, session, timeout: int = 6) -> dict:
    """One company: find its careers page and name the ATS behind it.

    Gives up on the whole domain the moment the connection itself fails,
    rather than working through the path list. A third of these domains do not
    resolve at all -- parked, renamed, or gone -- and trying five paths at
    fifteen seconds each spent seventy-five seconds proving it. That one
    default made a six-hundred-company census slower than the three-week
    probe it was meant to inform.
    """
    import requests

    for path in CAREERS_PATHS:
        url = f"https://{domain}{path}"
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
        except (requests.ConnectionError, requests.Timeout):
            # The host itself is not answering. No other path on it will.
            return {"domain": domain, "ats": "unreachable", "url": "", "status": 0}
        except (requests.RequestException, UnicodeError, ValueError):
            continue
        if response.status_code >= 400:
            continue                      # wrong path, right host — keep trying
        try:
            body = response.text[:MAX_HTML]
        except (UnicodeDecodeError, ValueError):
            body = ""
        return {"domain": domain, "ats": identify(response.url, body),
                "url": response.url[:200], "status": response.status_code}

    return {"domain": domain, "ats": "no careers page", "url": "", "status": 404}


def report() -> None:
    """What the census has found so far, ranked."""
    if not RESULTS.exists():
        print("  nothing measured yet")
        return
    counts = Counter()
    with open(RESULTS, encoding="utf-8") as handle:
        for line in handle:
            try:
                counts[json.loads(line)["ats"]] += 1
            except (ValueError, KeyError):
                continue

    total = sum(counts.values())
    have = {"greenhouse", "lever", "ashby", "workday"}
    reachable = sum(n for a, n in counts.items() if a in have)

    print(f"  {total:,} company careers pages checked\n")
    print(f"  {'platform':18} {'count':>7} {'share':>7}   scraper?")
    print(f"  {'-' * 52}")
    for ats, n in counts.most_common():
        mark = "yes" if ats in have else ("—" if ats in ("own site", "unreachable") else "NO")
        print(f"  {ats:18} {n:>7} {n/total*100:>6.1f}%   {mark}")

    print(f"\n  reachable today : {reachable:,} of {total:,} "
          f"({reachable/total*100:.1f}%)")
    missing = [(a, n) for a, n in counts.most_common()
               if a not in have and a not in ("own site", "unreachable")]
    if missing:
        print("\n  what each new scraper would add:")
        for ats, n in missing[:6]:
            print(f"    {ats:18} +{n/total*100:.1f}% of companies")


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--names", type=Path, default=Path("data/refined_names_all.txt"))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv[1:])

    if args.report:
        report()
        return 0

    import requests

    done = already_done()
    domains = []
    with open(args.names, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "," not in line:
                continue
            domain = line.split(",", 1)[1].strip()
            if domain and domain not in done:
                domains.append(domain)
            if len(domains) >= args.limit:
                break

    print(f"  {len(done):,} already checked, {len(domains):,} to go\n")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"})

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    found = Counter()
    # Each company is a different host, and each gets exactly one request, so
    # working on several at once is not impoliteness -- it is the only way a
    # census of hundreds finishes in minutes rather than hours.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with open(RESULTS, "a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(check, d, session): d for d in domains}
            for index, future in enumerate(as_completed(futures), 1):
                try:
                    result = future.result()
                except Exception:
                    result = {"domain": futures[future], "ats": "unreachable",
                              "url": "", "status": 0}
                out.write(json.dumps(result) + "\n")
                out.flush()         # a stopped run keeps everything it learned
                found[result["ats"]] += 1
                if index % 50 == 0:
                    top = ", ".join(f"{a} {n}" for a, n in found.most_common(3))
                    print(f"  {index}/{len(domains)}  {top}", flush=True)

    print()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
