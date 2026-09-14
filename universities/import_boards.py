"""Turn the campus careers directory into boards this tool can already scrape.

`universities/data/universities_careers.csv` holds every active US college and
trade school in the federal IPEDS directory, each with a careers page that was
opened and checked. Most of those pages are the school's own HTML, but a few
hundred are a hosted ATS — and where that ATS is one of the four this tool
already speaks, the board needs no new code at all. It needs importing.

Universities matter here out of proportion to their number. One careers page
carries a nursing job, a machinist job, a payroll job, a librarian job and a
professorship at the same time, which is exactly the breadth this tool spent
its first year not having.

Three things this module is careful about, each learned from the data:

**A board is not a school.** Four University of Wisconsin campuses post to one
Workday site; five American Institute campuses share one Greenhouse board. The
48 Workday rows are 41 distinct boards. Import the board once, under the name
of the system that owns it, or the scrape fetches the same postings four times.

**The ATS column can be wrong.** "Kenny's Academy of Barbering" is listed
against `jobs.lever.co/envato-2` — Envato is an Australian marketplace. That
board is real and live, so asking whether it answers proves nothing. The guard
has to be whether the slug looks anything like the school.

**A URL can name a board that no longer serves one**, so every slug is checked
against the board itself before it is added, the same way
`companies/discover_workday.py` does it.

    python -m universities.import_boards --dry-run
    python -m universities.import_boards
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "universities_careers.csv"

# The ATS names used by the directory, mapped to this tool's scraper keys.
# Everything absent from this table needs a scraper that does not exist yet;
# `--report` lists those, ranked, so the next one to write is never a guess.
SUPPORTED = {
    "Workday": "workday",
    "Greenhouse": "greenhouse",
    "Lever": "lever",
    "Ashby": "ashby",
}

_SLUG_PATTERNS = {
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I),
}

# Words that appear in so many school names that sharing one proves nothing.
_STOPWORDS = {
    "of", "the", "at", "and", "for", "in", "inc", "llc", "system", "campus",
}


def tokens(name: str) -> set[str]:
    """The meaningful words in a school's name, lowercased."""
    words = re.split(r"[^a-z0-9]+", name.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def acronyms(name: str) -> set[str]:
    """Initialisms a school might name its board after.

    Schools overwhelmingly do this — njit, nku, unf, wit, sju, uasys. A check
    that only compared whole words rejected every one of them, which is 20
    real boards thrown away to catch two wrong ones.
    """
    # One-letter fragments are punctuation debris, not initials: "Joseph's"
    # splits into "joseph" + "s", which turns sju into sjsu.
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if len(w) > 1]
    initials = "".join(w[0] for w in words)
    meaty = "".join(w[0] for w in words if w not in _STOPWORDS)
    out = {initials, meaty}
    # "University of Arkansas System" -> "uasys": initials plus a trailing word.
    if len(words) > 1:
        out.add(meaty[:-1] + words[-1])
        out.add(initials[:-1] + words[-1])
    return {a for a in out if len(a) >= 2}


def domain_root(domain: str) -> str:
    """"njit.edu" -> "njit"."""
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", (domain or "").strip().lower())
    return re.sub(r"[^a-z0-9]", "", host.split("/")[0].split(".")[0])


def plausible(school: str, slug: str, domain: str = "") -> bool:
    """Does this slug look like it belongs to this school?

    A live board proves the slug is somebody's, not that it is theirs:
    `jobs.lever.co/envato-2` is listed under a barbering academy, and Envato is
    an Australian marketplace that certainly answers. So liveness cannot be the
    guard — resemblance has to be.

    Three ways a board can resemble its school, and it only takes one. The
    school's own web domain is the strongest (`njit.edu` -> `njit`), an
    initialism is next (`uasys`, `nku`), and a shared word is the loosest. The
    bar is deliberately low: the only thing it must catch is a slug with no
    relationship to the school at all.
    """
    tenant = slug.split(":")[0].lower()
    flat = re.sub(r"[^a-z0-9]", "", tenant)
    if not flat:
        return False

    root = domain_root(domain)
    if root and (root in flat or flat in root):
        return True

    if flat in acronyms(school):
        return True

    for word in tokens(school):
        if len(word) > 3 and (word in flat or flat in word):
            return True
    return False


@dataclass
class Board:
    """One scrapeable board, and every campus that posts to it."""

    source: str
    slug: str
    schools: list[str] = field(default_factory=list)
    unitids: list[str] = field(default_factory=list)
    resembles: bool = False

    @property
    def name(self) -> str:
        """What to call the board, which is not what to call any one campus.

        Four University of Wisconsin campuses post to one site. Labelling it
        "University of Wisconsin-Parkside" is wrong three times over, so where
        the campuses share an opening phrase, that phrase is the board's name:
        "University of Wisconsin", "American Institute", "Centura College".
        """
        if not self.schools:
            return self.slug
        if len(self.schools) == 1:
            return self.schools[0]

        words = [re.split(r"(?<=[\w)])[\s\-–,]+", s) for s in self.schools]
        shared: list[str] = []
        for parts in zip(*words):
            if len({p.lower() for p in parts}) != 1:
                break
            shared.append(parts[0])
        # "University of" alone names nothing; fall back to the shortest campus.
        if shared and any(w.lower() not in _STOPWORDS for w in shared[-1:]):
            return " ".join(shared)
        return min(self.schools, key=len)

    @property
    def doubtful(self) -> bool:
        """One school, and a slug that looks nothing like it. Worth a look."""
        return len(self.schools) == 1 and not self.resembles


def slug_for(source: str, url: str) -> str | None:
    if source == "workday":
        from scraper.workday import slug_from_url

        return slug_from_url(url)
    found = _SLUG_PATTERNS[source].search(url or "")
    return found.group(1).lower() if found else None


def read_rows(path: Path = DATA) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return [r for r in csv.DictReader(handle) if (r.get("jobs_page_url") or "").strip()]


def collect(rows: list[dict]) -> tuple[list[Board], list[tuple[str, str, str]]]:
    """Distinct boards, and the (school, slug, reason) rows that were dropped.

    Resemblance is recorded on the board rather than used to drop it, because
    the dominant case for a slug that looks nothing like its school is not a
    mistake — it is a state system. `uasys` is the University of Arkansas
    System, `marylandconnect` is the University System of Maryland, `chess` is
    a New Mexico consortium. Dropping those threw away 18 real boards to catch
    one wrong one, and a lost university board costs far more here than a
    mislabelled company row, which a person can fix by reading one line.

    A board several different schools point at is a system board by definition
    — a misattribution is not shared — so those need no resemblance at all.
    """
    boards: OrderedDict[tuple[str, str], Board] = OrderedDict()
    dropped: list[tuple[str, str, str]] = []

    for row in rows:
        source = SUPPORTED.get((row.get("jobs_page_ats") or "").strip())
        if source is None:
            continue
        url = row["jobs_page_url"].strip()
        slug = slug_for(source, url)
        if not slug:
            dropped.append((row["name"], url, "no slug in the URL"))
            continue

        board = boards.setdefault((source, slug), Board(source=source, slug=slug))
        board.schools.append(row["name"])
        board.unitids.append(row.get("unitid", ""))
        if plausible(row["name"], slug, row.get("domain", "")):
            board.resembles = True

    return list(boards.values()), dropped


def unsupported_tally(rows: list[dict]) -> list[tuple[str, int]]:
    """ATS platforms we cannot scrape yet, by how many schools use them."""
    counts: dict[str, int] = {}
    for row in rows:
        ats = (row.get("jobs_page_ats") or "").strip()
        if ats and ats not in SUPPORTED:
            counts[ats] = counts.get(ats, 0) + 1
    return sorted(counts.items(), key=lambda pair: pair[1], reverse=True)


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be added, write nothing")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip asking each board whether it still answers")
    ap.add_argument("--csv", type=Path, default=DATA)
    ap.add_argument("--report", action="store_true",
                    help="Also list the platforms that still need a scraper")
    ap.add_argument("--strict", action="store_true",
                    help="Skip boards whose slug does not resemble their school")
    args = ap.parse_args(argv[1:])

    rows = read_rows(args.csv)
    boards, dropped = collect(rows)
    campuses = sum(len(b.schools) for b in boards)
    print(f"{len(rows):,} schools with a checked careers page")
    print(f"{campuses} of them post to {len(boards)} distinct boards we can already scrape")

    for source in SUPPORTED.values():
        count = sum(1 for b in boards if b.source == source)
        if count:
            print(f"    {source:12} {count}")

    if dropped:
        print(f"\n{len(dropped)} unparseable:")
        for school, what, why in dropped:
            print(f"    {school[:34]:36} {what[:34]:36} {why}")

    doubtful = [b for b in boards if b.doubtful]
    if doubtful:
        print(f"\n{len(doubtful)} name{'s' if len(doubtful) > 1 else ''} worth checking — "
              "one school, and a slug that looks nothing like it.")
        print("  Most are state systems, which is fine. A few are the directory\'s own\n"
              "  mistakes, and those import a real board under the wrong school\'s name.")
        for board in doubtful:
            print(f"    {board.source}/{board.slug[:36]:38} listed as {board.name[:32]}")
        if args.strict:
            boards = [b for b in boards if not b.doubtful]
            print(f"  --strict: skipping all {len(doubtful)}.")

    if args.report:
        print("\nPlatforms with no scraper yet, by schools using them:")
        for ats, count in unsupported_tally(rows):
            print(f"    {ats:22} {count:4}")

    from config.loader import load_config
    from db.session import init_engine
    from scraper.discovery import record_probe, upsert_company
    from scraper.http_client import HttpSettings, PoliteClient
    from scraper.runner import SCRAPER_TYPES

    cfg = load_config()
    init_engine(cfg.database_url)
    client = PoliteClient(HttpSettings.from_config(cfg))
    scrapers = {name: cls(client) for name, cls in SCRAPER_TYPES.items()}

    added = dead = 0
    print()
    for board in boards:
        scraper = scrapers[board.source]
        if not args.no_verify and not scraper.board_exists(board.slug):
            dead += 1
            print(f"  dead  {board.source:11} {board.slug[:40]:42} {board.name[:30]}")
            if not args.dry_run:
                record_probe(board.source, board.slug, False, board.name)
            continue

        shared = f"  (+{len(board.schools) - 1} campuses)" if len(board.schools) > 1 else ""
        print(f"  ok    {board.source:11} {board.slug[:40]:42} {board.name[:30]}{shared}")
        added += 1
        if not args.dry_run:
            upsert_company(board.name, board.slug, board.source, origin="universities",
                           board_url=scraper.board_url(board.slug))
            record_probe(board.source, board.slug, True, board.name)

    verb = "would add" if args.dry_run else "added"
    print(f"\ndone: {verb} {added} boards, {dead} no longer answer, {len(dropped)} unparseable")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
