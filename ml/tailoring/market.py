"""What the person's best-matching jobs ask for, across all of them.

One posting's gaps say what to learn for that job. Across the few hundred
jobs that fit the person best, the same counting says what to learn next:
"Dynatrace: asked by 41 of 300 jobs" is a better use of a weekend than a
skill one posting mentions once. The other half is just as useful: the
skills the person has that the most jobs ask for are the ones to lead with.

No model: the posting's scored terms (the same as the ATS score and the
keyword tracker), counted across jobs. A roadmap for one skill is a model
call, on request (ml/tailoring/roadmap.py).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

CACHE = PROJECT_ROOT / "data" / "tailoring" / "market.json"
ROADMAPS = PROJECT_ROOT / "data" / "tailoring" / "market_roadmaps.json"
DEFAULT_JOBS = 300
VERSION = 4               # a saved scan made by older rules is scanned again
# Words a posting capitalises in job titles, never part of a skill's name.
_TITLE_WORDS = {"engineer", "engineering", "developer", "pipeline", "pipelines", "operations", "team",
                "teams", "platform", "experience", "manager", "lead", "specialist"}
_NOT_STRENGTHS = {"engineer", "code", "backend", "frontend", "engineering", "developer"}
MIN_SHARE = 0.02          # asked by at least 2% of the jobs scanned (and two jobs), or it is noise


def _sentence(term: str, text: str) -> str:
    """The posting's own sentence that names the term, cut to a readable length."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", s.lower()):
            s = s.strip(" -•*\t")
            return s if len(s) <= 220 else s[:217].rsplit(" ", 1)[0] + "…"
    return ""


# The same skill under another name: a resume with CI/CD has continuous integration.
SAME = {"continuous integration": {"ci", "ci/cd"}, "continuous deployment": {"cd", "ci/cd"},
        "continuous delivery": {"cd", "ci/cd"}, "google cloud": {"gcp"}, "gcp": {"google cloud"},
        "postgres": {"postgresql"}, "postgresql": {"postgres"}, "k8s": {"kubernetes"},
        "amazon web services": {"aws"}, "microsoft azure": {"azure"}, "golang": {"go"},
        "javascript": {"js"}, "typescript": {"ts"}, "infrastructure as code": {"iac", "terraform"}}


def _is_skill(term: str, contexts: list[str]) -> bool:
    """A tool or named skill, not an ordinary word ("support", "fantastic", "citizens").

    A known skill or skill phrase; else a name the postings write beside a
    known skill ("Istio, Linkerd" in a sentence about Kubernetes) -- the rule
    the ATS gap list uses (ml/resume/ats.py).
    """
    from ml.resume.ats import FILLER, is_tool
    from ml.resume.emphasis import GENERIC
    from ml.resume.scorer import PHRASES, SKILL_TERMS
    if term in GENERIC or term in FILLER:
        return False
    if term in SKILL_TERMS or term in PHRASES:
        return True
    return any(is_tool(term, c) for c in contexts)


def _full_name(term: str, contexts: list[str]) -> str:
    """"active" -> "Active Directory": the name as the postings write it."""
    for c in contexts:
        # The term in any case, then capitalised words only: "Active Directory", not
        # "SaaS platform that powers the world".
        m = re.search(rf"(?<![A-Za-z0-9])(?i:{re.escape(term)})((?: [A-Z][\w+#.-]*)+)", c)
        if not (m and m.group(0)[:1].isupper()):
            continue
        words = m.group(0).strip(" .,").split()
        keep = [words[0]]
        for w in words[1:]:
            if w.lower().strip(".,") in _TITLE_WORDS or len(keep) >= 3:
                break
            keep.append(w)
        if len(keep) > 1:
            return " ".join(keep)
    return term


def scan(master_text: str, jobs: list[dict]) -> dict:
    """Count every scored term across `jobs` ({id, title, company, description, score}).

    Returns the skills the resume lacks and the ones it has, each with how
    many jobs ask, the share, example jobs and a sentence from a posting.
    """
    from ml.tailoring import keywords
    have = keywords._has(master_text)
    stats: dict[str, dict] = {}
    for job in jobs:
        jd = f"{job['title']}\n\n{job.get('description') or ''}"
        for term, weight in keywords.weights(jd, job.get("company")).items():
            e = stats.setdefault(term, {"term": term, "jobs": 0, "weight": 0.0, "examples": [], "contexts": []})
            e["jobs"] += 1
            e["weight"] += weight
            if len(e["examples"]) < 6:
                e["examples"].append({"id": job["id"], "title": job["title"], "company": job.get("company") or "",
                                      "score": job.get("score")})
            if len(e["contexts"]) < 3:
                sentence = _sentence(term, jd)
                if sentence and sentence not in e["contexts"]:
                    e["contexts"].append(sentence)
    n = len(jobs) or 1
    floor = max(2, int(n * MIN_SHARE))
    master_low = (master_text or "").lower()
    rows = []
    for e in stats.values():
        if e["jobs"] < floor:
            continue
        e["share"] = round(100 * e["jobs"] / n)
        e["weight"] = round(e["weight"], 2)
        e["skill"] = _is_skill(e["term"], e["contexts"])
        e["name"] = _full_name(e["term"], e["contexts"]) if e["skill"] and e["term"] not in have else e["term"]
        if e["name"] == e["term"]:                       # as the postings write it: MongoDB, Istio
            m = next((re.search(rf"(?<![A-Za-z0-9])(?i:{re.escape(e['term'])})(?![A-Za-z0-9])", c)
                      for c in e["contexts"] if re.search(rf"(?i:{re.escape(e['term'])})", c)), None)
            e["name"] = m.group(0) if m else e["term"]
        from ml.resume.scorer import SKILL_TERMS
        extra = (set(re.findall(r"[a-z0-9+#.]+", e["name"].lower())) - {e["term"]}) & SKILL_TERMS
        # "Microsoft Azure" for someone whose resume says Azure: they have it.
        same = SAME.get(e["term"], set()) | SAME.get(e["name"].lower(), set())
        e["have"] = (e["term"] in have or e["name"].lower() in master_low or bool(extra & have)
                     or bool(same & have) or any(f" {w} " in f" {master_low} " for w in same))
        rows.append(e)
    # CI and CD are one skill to a reader.
    by_term = {e["term"]: e for e in rows}
    if "ci" in by_term and "cd" in by_term:
        ci = by_term["ci"]
        ci["name"], ci["jobs"] = "CI/CD", max(ci["jobs"], by_term["cd"]["jobs"])
        ci["share"] = round(100 * ci["jobs"] / n)
        rows.remove(by_term["cd"])
    rows.sort(key=lambda e: (-e["jobs"], -e["weight"]))
    return {
        "jobs_scanned": len(jobs),
        "made": datetime.now().isoformat(timespec="minutes"),
        # Only tools and named skills: "support" and "fantastic" are in many postings too.
        "missing": [e for e in rows if not e["have"] and e["skill"]][:80],
        "strengths": [e for e in rows if e["have"] and e["skill"] and e["term"] not in _NOT_STRENGTHS][:40],
    }


def best_jobs(limit: int = DEFAULT_JOBS) -> list[dict]:
    """The open jobs that fit best, with a description long enough to read."""
    import sqlite3
    db = sqlite3.connect(f"file:{PROJECT_ROOT / 'data' / 'jobs.db'}?mode=ro", uri=True)
    rows = db.execute(
        "select id, title, company, description, ats_match_score from jobs "
        "where is_open = 1 and ats_match_score is not null and length(description) > 400 "
        "order by ats_match_score desc limit ?", (int(limit),)).fetchall()
    return [{"id": r[0], "title": r[1], "company": r[2], "description": r[3],
             "score": round(r[4] * 100) if r[4] is not None else None} for r in rows]


def master_text(cfg) -> tuple[str, str]:
    """(text, file name) of the person's master resume."""
    from ml.resume.pipeline import base_resume_path
    from ml.tailoring.reader import read_document
    path = base_resume_path(cfg)
    return read_document(path).text, path.name


def load_or_scan(cfg, limit: int = DEFAULT_JOBS, refresh: bool = False) -> dict:
    """The saved scan, or a new one when asked, when the scope changed, or when the resume did."""
    text, name = master_text(cfg)
    stamp = f"v{VERSION}:{name}:{len(text)}:{limit}"
    if not refresh and CACHE.exists():
        try:
            saved = json.loads(CACHE.read_text())
            if saved.get("stamp") == stamp:
                return saved
        except ValueError:
            pass
    out = scan(text, best_jobs(limit))
    out.update(stamp=stamp, limit=limit, resume=name)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out))
    return out


def saved_roadmaps() -> dict:
    try:
        return json.loads(ROADMAPS.read_text())
    except (OSError, ValueError):
        return {}


def roadmap_for_skill(term: str, cfg) -> dict:
    """A roadmap for one skill many jobs ask for, from the postings' own sentences. Kept."""
    from ml.tailoring import roadmap
    scanned = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    entry = next((e for e in scanned.get("missing", []) + scanned.get("strengths", []) if e["term"] == term), None)
    if entry is None:
        raise ValueError(f"“{term}” is not in the latest scan. Scan again, then try.")
    text, _ = master_text(cfg)
    jd = (f"{entry['jobs']} of the person's {scanned.get('jobs_scanned')} best-matching jobs ask for "
          f"{term}. How those postings put it:\n" + "\n".join(f"- {c}" for c in entry["contexts"]))
    gap = [{"skill": term, "priority": f"asked by {entry['jobs']} jobs", "status": "not_found",
            "context": "", "evidence": []}]
    made = roadmap.build(text, jd, gap, cfg)
    if not made:
        raise RuntimeError("The model wrote no roadmap.")
    item = made[0] | {"jobs": entry["jobs"], "made": datetime.now().isoformat(timespec="minutes")}
    kept = saved_roadmaps()
    kept[term] = item
    ROADMAPS.parent.mkdir(parents=True, exist_ok=True)
    ROADMAPS.write_text(json.dumps(kept, indent=1))
    return item
