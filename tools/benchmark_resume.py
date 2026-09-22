"""Measure the tailoring pipeline against the criteria resume tools are judged on.

Not a unit test. This runs the real pipeline against real postings with the
configured model and reports numbers, because "it works" is an opinion and a
pass rate is not.

The criteria are the ones any resume product is assessed on -- parseability,
formatting, tailoring quality, speed, cost -- plus one that the market tools
do not offer at all: whether the document makes a claim the person cannot
support. That last one is the reason this project exists, so it is measured
first and weighted hardest.

    python -m tools.benchmark_resume --jobs 2636 2848 2683
    python -m tools.benchmark_resume --top 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

BULLET = "•"


# -- the checks -------------------------------------------------------------

def check_parseable(path: Path) -> dict:
    """Can an ATS read it back? The question every parser actually asks.

    A resume that renders beautifully and parses to nothing is worth less than
    a plain one that parses, so this reads the .docx the way a parser would --
    text only, no styling -- and looks for the things a parser must find.
    """
    from docx import Document

    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    found = {
        "contact_email": bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)),
        "contact_phone": bool(re.search(r"[\+\(]?\d[\d\s().-]{7,}\d", text)),
        "experience_section": any("EXPERIENCE" in l.upper() for l in lines),
        "education_section": any("EDUCATION" in l.upper() for l in lines),
        "skills_section": any(
            k in l.upper() for l in lines for k in ("SKILL", "EXPERTISE", "TECHNICAL")),
        "dates_present": bool(re.search(r"(19|20)\d{2}", text)),
    }
    # The structures that actually break parsers, not the ones that look risky.
    hazards = {
        "tables": len(doc.tables),
        "images": len(doc.inline_shapes),
        "header_text": sum(1 for s in doc.sections
                           for p in s.header.paragraphs if p.text.strip()),
        "footer_text": sum(1 for s in doc.sections
                           for p in s.footer.paragraphs if p.text.strip()),
        "text_boxes": len(doc.element.body.findall(
            ".//{urn:schemas-microsoft-com:vml}textbox")),
    }
    return {"found": found, "hazards": hazards,
            "score": sum(found.values()) / len(found),
            "clean": all(v == 0 for v in hazards.values())}


def check_formatting(path: Path) -> dict:
    """Is it typeset, or merely typed?

    Each of these was a real defect in this project's first writer, which is
    why they are measured rather than assumed.
    """
    from docx import Document

    doc = Document(path)
    body = [p for p in doc.paragraphs if p.text.strip()]
    bullets = [p for p in body if p.text.strip().startswith(BULLET)]
    headings = [p for p in body if p.style.name.startswith("Heading")]
    hanging = [p for p in bullets if (p.paragraph_format.first_line_indent or 0) < 0]
    tabbed = [p for p in body if len(p.paragraph_format.tab_stops) > 0]
    kept = [p for p in headings if p.paragraph_format.keep_with_next]
    # A long line outside a list, once the header block is past, is a bullet
    # that lost its marker -- the exact half-marked-list defect.
    orphans = [p for p in body[3:] if len(p.text.strip()) > 80
               and not p.text.strip().startswith(BULLET)
               and "\t" not in p.text and not p.style.name.startswith("Heading")]
    return {
        "paragraphs": len(body),
        "headings": len(headings),
        "bullets": len(bullets),
        "bullets_hanging": len(hanging),
        "hanging_rate": (len(hanging) / len(bullets)) if bullets else 1.0,
        "headings_kept_with_next": len(kept),
        "date_tab_stops": len(tabbed),
        "unbulleted_long_lines": len(orphans),
    }


META = re.compile(
    r"demonstrat\w+ (ability|experience)|mirror\w*|directly support\w*"
    r"|showcas\w+|aligning with|which aligns|as required by (the|this) role", re.I)


def check_tailoring(review: Path) -> dict:
    """Did the rewrite follow the rules, or pad?

    Reads the review note rather than the .docx, because it holds the before
    and after of every bullet -- which is what makes tense and length
    measurable at all.
    """
    if not review.exists():
        return {}
    text = review.read_text(encoding="utf-8", errors="replace")
    pairs = re.findall(r"FROM: (.+?)\n\s+TO:\s+(.+?)\n", text)
    if not pairs:
        return {"rewrites": 0}

    longer = tense = meta = unchanged = 0
    words = 0
    for before, after in pairs:
        b, a = before.split(), after.split()
        words += len(a) - len(b)
        if len(a) > len(b):
            longer += 1
        if b and a and b[0].lower() != a[0].lower() \
           and b[0].lower().rstrip("d") == a[0].lower().rstrip("d"):
            tense += 1          # "Analyze" -> "Analyzed" on a current role
        if META.search(after):
            meta += 1
        if before.strip() == after.strip():
            unchanged += 1
    gaps = len(re.findall(r"^  - ", text, re.M))
    return {
        "rewrites": len(pairs),
        "longer_than_original": longer,
        "tense_changed": tense,
        "meta_clauses": meta,
        "returned_unchanged": unchanged,
        "net_words": words,
        "gaps_named": gaps,
    }


def run_one(job_id: int, out_dir: Path) -> dict:
    """Tailor one posting end to end and measure what came out."""
    from config.loader import load_config
    from db.models import Job
    from db.session import get_session, init_engine
    from resume.pipeline import load_base_resume
    from resume.guard import check_no_fabrication
    from resume.letter import SYSTEM_PROMPT as LETTER_PROMPT
    from resume.tailor import tailor_resume
    from resume.writer import output_filename, write_review_note, write_tailored_resume

    cfg = load_config()
    init_engine(cfg.database_url)
    with get_session() as session:
        job = session.get(Job, job_id)
        title, company = job.title, job.company
        jd, external = job.description or "", job.external_id or ""

    resume = load_base_resume(cfg)
    started = time.time()
    result = tailor_resume(resume, title, company, jd, cfg=cfg)
    elapsed = time.time() - started

    stem = output_filename(company, title, external)[:-5]
    docx = out_dir / f"{stem}.docx"
    review = out_dir / f"{stem}_review.txt"
    write_tailored_resume(resume, result, docx)
    write_review_note(result, review, jd)

    guard = result.guard or check_no_fabrication(resume.text(), result.tailored_text())
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "seconds": round(elapsed, 1),
        "free": getattr(result.cost, "usd", 0) in (0, None),
        "guard_ok": guard.ok,
        "guard_violations": [str(v) for v in guard.violations][:4],
        "parse": check_parseable(docx),
        "format": check_formatting(docx),
        "tailor": check_tailoring(review),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, nargs="*", default=[])
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("data/benchmark"))
    args = ap.parse_args(argv[1:])

    from config.loader import load_config
    from db.session import get_session, init_engine
    from db.models import Job

    ids = list(args.jobs)
    if args.top:
        cfg = load_config()
        init_engine(cfg.database_url)
        with get_session() as session:
            ids += [j.id for j in session.query(Job)
                    .filter(Job.is_open.is_(True), Job.description.isnot(None))
                    .order_by(Job.ats_match_score.desc()).limit(args.top)]
    if not ids:
        print("Nothing to benchmark. Pass --jobs or --top.")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for job_id in dict.fromkeys(ids):
        print(f"  running {job_id} ...", flush=True)
        try:
            results.append(run_one(job_id, args.out))
        except Exception as exc:          # a benchmark reports failures, not hides them
            results.append({"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"})

    (args.out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n  wrote {args.out / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
