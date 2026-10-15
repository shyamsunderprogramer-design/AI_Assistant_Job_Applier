"""Build a resume from scratch: a guided form, drawn by the same writers as a tailored resume.

For someone with no resume yet, or one too far gone to fix. The person fills
in who they are, each job (title, employer, place, dates, and either finished
bullets or rough notes), skills in groups, education, projects,
certifications and any other section; the page saves it as they go.

Drawn by ml/resume/writer.py and ml/resume/pdf.py from a Resume made here,
in any template, so a built resume and a tailored one are the same document
-- and the built one can become the master resume that every tailoring
starts from.

The model helps on request, and never invents:

  * notes -> bullets: "migrated jenkins to github actions, 40 repos, builds
    went from 25 min to 8" becomes "Migrated 40 repositories from Jenkins to
    GitHub Actions, cutting build time from 25 to 8 minutes." Every number
    and named tool in a bullet is checked against the notes and the rest of
    the form, and anything not there is flagged on the bullet. When a number
    would make a bullet stronger the model asks for it instead of guessing.
  * one bullet made stronger, checked the same way;
  * a summary from the form's own facts, checked the same way.

Skills the person mentions in their jobs but has not listed are suggested
without a model. Each job's bullets get the health check's bullet rules as
they are typed (ml/resume/health.py).

Saved in data/builder/ (ignored by git: it is a resume).
"""

from __future__ import annotations

import calendar
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

FOLDER = PROJECT_ROOT / "data" / "builder"
DATA = FOLDER / "resume.json"
OUT = FOLDER / "out"
DOCX = OUT / "resume.docx"
PDF = OUT / "resume.pdf"
BACKUPS = FOLDER / "replaced"

SECTIONS = ("contact", "summary", "experience", "skills", "education", "projects", "certifications", "extra")


def empty() -> dict:
    return {
        "contact": {"name": "", "title": "", "email": "", "phone": "", "city": "", "region": "",
                    "linkedin": "", "website": "", "relocate": False},
        "summary": "",
        "experience": [],
        "skills": [],
        "education": [],
        "projects": [],
        "certifications": [],
        "extra": [],
        "template": "classic",
        "look": {},
        "updated": "",
    }


def new_job() -> dict:
    return {"id": _id(), "title": "", "company": "", "location": "", "start": "", "end": "",
            "current": False, "notes": "", "bullets": []}


def _id() -> str:
    return uuid.uuid4().hex[:8]


# -- saving --------------------------------------------------------------------------------

def _text(v, limit=2000) -> str:
    return " ".join(str(v or "").split())[:limit] if not isinstance(v, (dict, list)) else ""


def _month(v) -> str:
    """"YYYY-MM", or "" for anything else."""
    v = str(v or "").strip()
    return v if re.fullmatch(r"(19|20)\d\d-(0[1-9]|1[0-2])", v) else ""


def _lines(v, limit=40) -> list[str]:
    if isinstance(v, str):
        v = v.splitlines()
    out = []
    for line in v or []:
        from ml.resume.parser import strip_bullet
        t = strip_bullet(_text(line, 600))
        if t:
            out.append(t)
    return out[:limit]


def clean(data: dict) -> dict:
    """Only the fields the form has, each the right shape: the page posts the whole thing."""
    out = empty()
    c = data.get("contact") or {}
    for k in out["contact"]:
        out["contact"][k] = bool(c.get(k)) if k == "relocate" else _text(c.get(k), 200)
    out["summary"] = str(data.get("summary") or "").strip()[:2500]
    for j in (data.get("experience") or [])[:30]:
        out["experience"].append({
            "id": _text(j.get("id"), 16) or _id(), "title": _text(j.get("title"), 120),
            "company": _text(j.get("company"), 120), "location": _text(j.get("location"), 80),
            "start": _month(j.get("start")), "end": "" if j.get("current") else _month(j.get("end")),
            "current": bool(j.get("current")), "notes": str(j.get("notes") or "").strip()[:4000],
            "bullets": _lines(j.get("bullets"))})
    for g in (data.get("skills") or [])[:20]:
        label, items = _text(g.get("label"), 60), _text(g.get("items"), 600)
        if label or items:
            out["skills"].append({"id": _text(g.get("id"), 16) or _id(), "label": label, "items": items})
    for e in (data.get("education") or [])[:10]:
        out["education"].append({
            "id": _text(e.get("id"), 16) or _id(), "degree": _text(e.get("degree"), 120),
            "field": _text(e.get("field"), 120), "school": _text(e.get("school"), 160),
            "location": _text(e.get("location"), 80), "start": _month(e.get("start")),
            "end": _month(e.get("end")), "details": _lines(e.get("details"), 6)})
    for p in (data.get("projects") or [])[:15]:
        out["projects"].append({
            "id": _text(p.get("id"), 16) or _id(), "name": _text(p.get("name"), 120),
            "link": _text(p.get("link"), 200), "start": _month(p.get("start")), "end": _month(p.get("end")),
            "notes": str(p.get("notes") or "").strip()[:3000], "bullets": _lines(p.get("bullets"), 10)})
    for c2 in (data.get("certifications") or [])[:20]:
        if _text(c2.get("name")):
            out["certifications"].append({"id": _text(c2.get("id"), 16) or _id(), "name": _text(c2.get("name"), 160),
                                          "issuer": _text(c2.get("issuer"), 120), "date": _month(c2.get("date"))})
    for x in (data.get("extra") or [])[:6]:
        heading = _text(x.get("heading"), 60)
        if heading:
            out["extra"].append({"id": _text(x.get("id"), 16) or _id(), "heading": heading,
                                 "lines": _lines(x.get("lines"), 15)})
    from ml.resume.style import TEMPLATES, clean_look
    out["template"] = data.get("template") if data.get("template") in TEMPLATES else "classic"
    out["look"] = clean_look(data.get("look"))
    return out


def load() -> dict:
    try:
        return clean(json.loads(DATA.read_text()))
    except (OSError, ValueError):
        return empty()


def save(data: dict) -> dict:
    data = clean(data)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    FOLDER.mkdir(parents=True, exist_ok=True)
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(DATA)
    return data


# -- the form as a Resume the writers draw ------------------------------------------------

def month_label(ym: str) -> str:
    return f"{calendar.month_abbr[int(ym[5:7])]} {ym[:4]}" if _month(ym) else ""


def span(start: str, end: str, current: bool = False) -> str:
    a, b = month_label(start), "Present" if current else month_label(end)
    if a and b:
        return f"{a} – {b}"
    return a or b


def _dated(head: str, dates: str) -> str:
    """The line the writers split: text, a tab, the dates."""
    return f"{head}\t{dates}" if dates else head


def header_lines(data: dict) -> list[str]:
    c = data["contact"]
    place = ", ".join(p for p in (c["city"], c["region"]) if p)
    if place and c["relocate"]:
        place += " (Open to Relocate)"
    linkedin = re.sub(r"^https?://(www\.)?", "", c["linkedin"]).split("?")[0].rstrip("/")
    contact = " · ".join(p for p in (place, c["phone"], c["email"], linkedin, c["website"]) if p)
    lines = [c["name"] or "Your Name"]
    if contact:
        lines.append(contact)
    if c["title"]:
        lines.append(c["title"])
    return lines


def to_resume(data: dict):
    """(Resume, header lines) for the writers: the same shapes a parsed resume has."""
    from ml.resume.parser import Resume, Section
    sections = [Section("HEADER", header_lines(data))]
    if data["summary"].strip():
        sections.append(Section("Summary", [p.strip() for p in data["summary"].split("\n") if p.strip()]))
    skills = [f"{g['label']}: {g['items']}" if g["label"] else g["items"] for g in data["skills"] if g["items"]]
    if skills:
        sections.append(Section("Skills", skills))
    jobs = []
    for j in data["experience"]:
        if not (j["title"] or j["company"]):
            continue
        head = ", ".join(p for p in (j["title"], j["company"], j["location"]) if p)
        jobs.append(_dated(head, span(j["start"], j["end"], j["current"])))
        jobs += [f"• {b}" for b in j["bullets"]]
    if jobs:
        sections.append(Section("Experience", jobs))
    projects = []
    for p in data["projects"]:
        if not p["name"]:
            continue
        head = p["name"] + (f" · {p['link']}" if p["link"] else "")
        projects.append(_dated(head, span(p["start"], p["end"])))
        projects += [f"• {b}" for b in p["bullets"]]
    if projects:
        sections.append(Section("Projects", projects))
    certs = [_dated(", ".join(x for x in (c["name"], c["issuer"]) if x), month_label(c["date"]))
             for c in data["certifications"]]
    if certs:
        sections.append(Section("Certifications", certs))
    for x in data["extra"]:
        if x["lines"]:
            sections.append(Section(x["heading"], list(x["lines"])))
    education = []
    for e in data["education"]:
        if not (e["degree"] or e["school"]):
            continue
        degree = " in ".join(p for p in (e["degree"], e["field"]) if p) or e["school"]
        education.append(_dated(degree, span(e["start"], e["end"])))
        school = ", ".join(p for p in (e["school"], e["location"]) if p)
        if school and degree != e["school"]:
            education.append(school)
        education += list(e["details"])
    if education:
        sections.append(Section("Education", education))
    raw = "\n".join(l for s in sections for l in ([s.heading] if s.heading != "HEADER" else []) + s.lines)
    return Resume(raw_text=raw, sections=sections), header_lines(data)


def plain_text(data: dict) -> str:
    """The resume as text: what the checks and the model read."""
    base, header = to_resume(data)
    return "\n".join(header) + "\n" + base.raw_text


def render(data: dict, template: str | None = None) -> dict:
    """The .docx and the PDF, page pictures and the page checks."""
    from ml.resume.pdf import write_pdf
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import plan_document, write_tailored_resume
    from ml.tailoring.documents import page_images, visual_checks
    base, header = to_resume(data)
    result = TailorResult(summary=None, template=template or data.get("template") or "classic",
                          look=dict(data.get("look") or {}))
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("page*.png"):
        old.unlink()
    write_tailored_resume(base, result, DOCX, header_lines=header)
    write_pdf(base, result, PDF, header_lines=header)
    pages = page_images(PDF, OUT, "page")
    headings = [t for k, t in plan_document(base, result, header) if k == "heading"]
    checks = visual_checks(PDF, "PDF", headings)
    return {"pages": [Path(p).name for p in pages], "count": len(pages),
            "checks": [{"page": c.page, "kind": c.kind, "message": c.message, "severity": c.severity}
                       for c in checks]}


# -- what is missing, and the health check on the form -------------------------------------

def gaps(data: dict) -> list[dict]:
    """What the form still needs, in the order to fix it: {where, message}."""
    out = []
    c = data["contact"]
    if not c["name"]:
        out.append({"where": "contact", "message": "Your name."})
    if not (c["email"] or c["phone"]):
        out.append({"where": "contact", "message": "An email or a phone number, so a recruiter can reach you."})
    elif not c["email"]:
        out.append({"where": "contact", "message": "An email address."})
    if not c["city"]:
        out.append({"where": "contact", "message": "A city: many applicant systems filter by location."})
    if not data["experience"] and not data["projects"]:
        out.append({"where": "experience", "message": "At least one job, internship or project."})
    for j in data["experience"]:
        name = j["title"] or j["company"] or "A job"
        if not j["title"] or not j["company"]:
            out.append({"where": f"job:{j['id']}", "message": f"{name}: the title and the employer."})
        if not j["start"]:
            out.append({"where": f"job:{j['id']}", "message": f"{name}: when it started."})
        if not j["current"] and not j["end"] and j["start"]:
            out.append({"where": f"job:{j['id']}", "message": f"{name}: when it ended, or tick “I work here now”."})
        if j["start"] and j["end"] and j["start"] > j["end"]:
            out.append({"where": f"job:{j['id']}", "message": f"{name}: it ends before it starts."})
        if not j["bullets"]:
            out.append({"where": f"job:{j['id']}", "message": f"{name}: what you did there "
                                                              "(write bullets, or notes and press Write bullets)."})
    if not data["skills"]:
        out.append({"where": "skills", "message": "Your skills, in groups (Cloud: AWS, Azure)."})
    if not data["education"]:
        out.append({"where": "education", "message": "Education (a degree, a course, or your school)."})
    return out


def job_checks(data: dict) -> dict:
    """The health check's bullet rules, for each job: {job id: [finding]}."""
    from dataclasses import asdict
    from ml.resume.health import bullet_findings
    return {j["id"]: [asdict(f) for f in bullet_findings(j["bullets"])] for j in data["experience"]}


def health(data: dict):
    """The whole health check, on the form."""
    from ml.resume import health as h
    base, header = to_resume(data)
    parts = h.Parts(name=data["contact"]["name"], contact_text="\n".join(header[1:] + header[:1]),
                    text=plain_text(data))
    for s in base.sections[1:]:
        kind = {"summary": "summary", "skills": "skills", "experience": "experience", "projects": "projects",
                "certifications": "certifications", "education": "education"}.get(s.heading.lower())
        parts.headings.append((s.heading, kind or "other"))
        parts.sections[kind or "other"] = s.lines
    parts.bullets = [b for j in data["experience"] for b in j["bullets"]] + \
                    [b for p in data["projects"] for b in p["bullets"]]
    parts.jobs = [{"header": f"{j['title']}, {j['company']}", "start": j["start"] or None,
                   "end": j["end"] or None, "present": j["current"], "dates": span(j["start"], j["end"], j["current"])}
                  for j in data["experience"]]
    words = len(parts.text.split())
    parts.worked_years = years_of(data)
    parts.pages = None if not PDF.exists() else _pdf_pages()
    report = h.check(parts)
    if words < 1:
        report.score = 0
    return report


def _pdf_pages() -> int | None:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(PDF))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return None


def suggest_skills(data: dict) -> list[str]:
    """Tools named in the jobs and projects that the skills section does not list. No model."""
    from ml.resume.scorer import PHRASES, SKILL_TERMS, tokenize
    listed = " ".join([f"{g['label']} {g['items']}" for g in data["skills"]]
                      + [l for x in data["extra"] for l in x["lines"]]).lower()
    listed_tokens = {t.strip(".-") for t in tokenize(listed)}
    text = "\n".join([*(b for j in data["experience"] for b in j["bullets"] + [j["notes"]]),
                      *(b for p in data["projects"] for b in p["bullets"] + [p["notes"]])])
    generic = {"api", "apis", "backend", "frontend", "fullstack", "architecture", "algorithms", "debugging",
               "profiling", "mentoring", "leadership", "scalability", "latency", "throughput", "caching",
               "observability", "ci", "cd", "devops", "sre", "etl", "rest"}
    found: dict[str, str] = {}
    for m in re.finditer(r"[A-Za-z][\w+#.-]*", text):
        word = m.group(0).rstrip(".")
        low = word.lower()
        if low in SKILL_TERMS and low not in generic and low not in listed_tokens and low not in found:
            found[low] = word if any(ch.isupper() for ch in word) else word.capitalize()
    low_text = text.lower()
    for phrase in PHRASES:
        if phrase in low_text and phrase not in listed and phrase not in found:
            found[phrase] = phrase
    return list(found.values())[:30]


# -- the model, on request: it only rewrites what the person wrote ---------------------------

BULLETS_SYSTEM = """You turn a person's rough notes about one job into resume bullets.
Reply with JSON only:
{"bullets": ["..."], "ask": ["questions whose answers would make a bullet stronger"]}
Rules:
- Use ONLY what the notes say. Never add a tool, number, team size, result, employer or duty that is
  not in the notes. If something is unclear, leave it out and ask.
- Each bullet: start with a strong action verb in the past tense for anything finished (Built, Cut, Led);
  only work still going on in a current job may use the plain present (Build, Lead), never "Builds",
  then what was done, then the result. Where the notes give a measure, use the shape
  "accomplished X, measured by Y, by doing Z". Never invent the measure.
- One sentence each, 12 to 30 words. No "I", "my", "we". No stock phrases ("results-driven").
  Plain words, no long dashes. Write tool names in their usual form (SQL, Tableau, Snowflake, AWS).
- 3 to 7 bullets, most impressive first. Do not repeat the same opening verb.
- "ask": up to 4 short questions for numbers or results the notes hint at but do not give
  ("How many repositories were moved?"). Empty if the notes are complete."""

ONE_SYSTEM = """You make one resume bullet stronger. Reply with JSON only:
{"rewrite": "the stronger bullet", "ask": "a question for a number that would make it stronger, or empty"}
Rules: use ONLY what the bullet and the job's notes say; never add a tool, number or result. Start with
a strong action verb, then what was done, then the result. One sentence, under 30 words, no pronouns,
no stock phrases, no long dashes."""

SUMMARY_SYSTEM = """You write the summary at the top of a resume from the resume's own content.
Reply with JSON only: {"summary": "...", "ask": "a question if something important is missing, or empty"}
Rules:
- 2 to 4 sentences, 45 to 85 words. Open with the title the person gives (or the latest job title)
  and their years of experience as stated below. Then their strongest areas and two concrete results
  taken from the bullets, with the numbers as written.
- Use ONLY facts in the resume below. Never add a tool, number, employer, title or result.
- No "I", "my". No stock phrases ("results-driven", "passionate", "team player"). No long dashes."""


def _source_text(data: dict, extra: str = "") -> str:
    """Everything the person wrote: what a generated line may draw on."""
    notes = "\n".join([j["notes"] for j in data["experience"]] + [p["notes"] for p in data["projects"]])
    return f"{plain_text(data)}\n{notes}\n{extra}"


# How a tool is written. Notes typed in a hurry say "tableau" and "sql"; a resume does not.
CANON = {w.lower(): w for w in (
    "SQL NoSQL MySQL PostgreSQL MongoDB Snowflake Tableau Excel Python Java JavaScript TypeScript AWS Azure GCP "
    "Kubernetes Docker Terraform Ansible Jenkins GitHub GitLab Git Linux Redis Kafka Spark Hadoop Airflow dbt "
    "Looker PowerBI Salesforce HubSpot Jira Confluence React Angular Vue Django Flask FastAPI Node.js "
    "GraphQL Pandas NumPy PyTorch TensorFlow Datadog Grafana Prometheus Splunk BigQuery Redshift Databricks "
    "DynamoDB Elasticsearch Helm ArgoCD macOS iOS Android Figma SAP Oracle Workday ServiceNow Photoshop").split()}
CANON.update({"power bi": "Power BI", "google analytics": "Google Analytics", "a/b": "A/B", "ci/cd": "CI/CD",
              "github actions": "GitHub Actions", "azure devops": "Azure DevOps", "vs code": "VS Code"})


def canonical(line: str) -> str:
    for low, name in sorted(CANON.items(), key=lambda kv: -len(kv[0])):
        line = re.sub(rf"(?<![\w/.-]){re.escape(low)}(?![\w/-])", name, line, flags=re.I)
    return line


def _checked(line: str, source: str) -> dict:
    from backend.apply.essays import numbers_not_on
    from ml.resume.health import tools_not_on
    line = " ".join(str(line or "").split()).replace(" — ", ", ").replace("—", ", ")
    line = canonical(line)
    return {"text": line, "unsupported_numbers": numbers_not_on(line, source),
            "unsupported_tools": tools_not_on(line, source)}


def _job(data: dict, kind: str, item_id: str) -> dict | None:
    return next((x for x in data.get(kind, []) if x["id"] == item_id), None)


def years_of(data: dict) -> float | None:
    """Years actually worked: the jobs' spans added up, overlaps counted once and gaps not at all.

    From the first start to today would count an internship four years ago
    and the gap after it as experience.
    """
    from datetime import date
    now = date.today().strftime("%Y-%m")
    spans = sorted((j["start"], now if j["current"] else (j["end"] or j["start"]))
                   for j in data["experience"] if j["start"])
    if not spans:
        return None

    def n(ym):
        return int(ym[:4]) * 12 + int(ym[5:7]) - 1

    months, reach = 0, None
    for start, end in spans:
        a, b = n(start), n(end) + 1                       # the end month counts
        if reach is not None and a < reach:
            a = reach
        if b > a:
            months += b - a
            reach = b if reach is None or b > reach else reach
    return round(months / 12, 1)


def write_bullets(data: dict, kind: str, item_id: str, cfg, ask=None) -> dict:
    """Bullets from one job's (or project's) notes: {bullets[{text, unsupported_*}], ask[]}."""
    item = _job(data, kind, item_id)
    if item is None:
        raise ValueError("That entry is not in the form. Save, then try again.")
    notes = (item.get("notes") or "").strip()
    existing = "\n".join(f"- {b}" for b in item.get("bullets") or [])
    if len(notes.split()) < 4 and not existing:
        raise ValueError("Write a few notes first: what you did there, with what, and what came of it.")
    if ask is None:
        from ml.tailoring.review import _ask as ask
    if kind == "experience":
        about = (f"JOB: {item['title']} at {item['company']}, {span(item['start'], item['end'], item['current'])}"
                 + (" (current job: present tense)" if item["current"] else ""))
    else:
        about = f"PROJECT: {item['name']}"
    body = ask(BULLETS_SYSTEM, f"{about}\n\nNOTES:\n{notes}\n\nBULLETS ALREADY WRITTEN (improve and keep "
                               f"their facts):\n{existing or '(none)'}", cfg)
    source = f"{about}\n{notes}\n{existing}"
    bullets = [_checked(b, source) for b in body.get("bullets") or [] if str(b).strip()][:8]
    questions = [str(q).strip() for q in body.get("ask") or [] if str(q).strip()][:4]
    return {"bullets": bullets, "ask": questions}


def improve_bullet(data: dict, kind: str, item_id: str, bullet: str, cfg, ask=None) -> dict:
    item = _job(data, kind, item_id) or {}
    if not bullet.strip():
        raise ValueError("No bullet given.")
    if ask is None:
        from ml.tailoring.review import _ask as ask
    notes = item.get("notes") or ""
    current = " (current job: present tense)" if item.get("current") else ""
    body = ask(ONE_SYSTEM, f"JOB NOTES{current}:\n{notes or '(none)'}\n\nBULLET:\n{bullet}", cfg)
    out = _checked(body.get("rewrite") or "", f"{notes}\n{bullet}")
    out["ask"] = str(body.get("ask") or "").strip()
    return out


def write_summary(data: dict, cfg, ask=None) -> dict:
    if not any(j["bullets"] for j in data["experience"]) and not data["skills"]:
        raise ValueError("Fill in a job or your skills first: the summary is written from them.")
    if ask is None:
        from ml.tailoring.review import _ask as ask
    yrs = years_of(data)
    told = f"Years of experience (from the job dates): {yrs:g}" if yrs is not None else "Years: not known; do not state any."
    body = ask(SUMMARY_SYSTEM, f"{told}\n\nRESUME:\n{plain_text(data)[:12000]}", cfg)
    out = _checked(body.get("summary") or "", _source_text(data, told))
    out["ask"] = str(body.get("ask") or "").strip()
    return out


# -- starting from a resume the person already has ---------------------------------------

def from_file(path: Path | str) -> dict:
    """The form filled from an existing resume: what could be read, nothing guessed.

    Jobs, their dates and their bullets come across as written; anything the
    reader was unsure of is listed so the person can check it.
    """
    from ml.tailoring.reader import read_document
    from ml.tailoring.structure import EMAIL, PHONE, URL, build
    from ml.resume.parser import strip_bullet
    extracted = read_document(path)
    model = build(extracted)
    data = empty()
    c = data["contact"]
    c["name"] = model.name
    c["email"] = (model.contact.get("emails") or [""])[0]
    phones = [p for p in model.contact.get("phones") or []]
    c["phone"] = phones[0] if phones else ""
    links = model.contact.get("links") or []
    c["linkedin"] = next((l for l in links if "linkedin" in l.lower()), "")
    c["website"] = next((l for l in links if "linkedin" not in l.lower() and "mailto" not in l.lower()), "")
    for f in model.facts:
        if f.section != "contact" or f.text.strip() == model.name:
            continue
        text = f.text
        if not (EMAIL.search(text) or PHONE.search(text)):
            if not c["title"] and len(text) < 160:
                c["title"] = text
            continue
        # The place: the first piece of the contact line that is not an email, phone or link.
        for piece in re.split(r"\s*[|·•]\s*", text):
            piece = piece.strip()
            if piece and not (EMAIL.search(piece) or PHONE.search(piece) or URL.search(piece)
                              or piece.lower() == "linkedin"):
                c["relocate"] = "relocat" in piece.lower()
                place = re.sub(r"\(.*?\)", "", piece).strip(" ,")
                bits = [b.strip() for b in place.split(",") if b.strip()]
                if bits:
                    if len(bits) >= 2 and len(bits[0]) > 2:
                        c["city"], c["region"] = bits[0], bits[1]
                    else:
                        c["region"] = ", ".join(bits)
                break
    data["summary"] = "\n".join(model.fact(i).text for i in model.sections.get("summary", []) if model.fact(i))
    # Each skills group as one row, "Cloud Platforms: AWS, Azure, GCP" (ml/resume/skills.py): a group's
    # label over its own bullets is one group, two groups run together on one line are two, and each
    # tool comes without the description written after it. A group written as sentences ("CI/CD:
    # Architected pipelines via ...") stays as written, still under Skills: kept apart under a second
    # "Skills" heading, it printed twice on every tailored resume (Oct 2026).
    from ml.resume import skills as skill_rows
    under = _headed_lines(extracted)
    for heading, kind, lines in under:
        if kind == "skills":
            for g in skill_rows.clean(skill_rows.groups(lines)):
                data["skills"].append({"id": _id(), "label": g.label,
                                       "items": g.items[0] if g.prose else ", ".join(g.items)})
        elif kind == "other" and lines:
            data["extra"].append({"id": _id(), "heading": heading.title() if heading.isupper() else heading,
                                  "lines": [strip_bullet(l) for l in lines]})
    for e in model.entries:
        facts = [model.fact(i) for i in e.facts[1:] if model.fact(i)]
        if e.section == "experience":
            title, company = e.title, e.employer
            location = ""
            bits = [b.strip() for b in company.split(",")]
            if len(bits) >= 2 and len(bits[-1]) <= 3:
                company, location = ", ".join(bits[:-1]), bits[-1]
            data["experience"].append({
                "id": _id(), "title": title, "company": company, "location": location,
                "start": e.start or "", "end": "" if e.present else (e.end or ""), "current": e.present,
                "notes": "", "bullets": [strip_bullet(f.text) for f in facts if f.kind in ("bullet", "text")]})
        elif e.section == "projects":
            data["projects"].append({"id": _id(), "name": e.title or e.header, "link": "",
                                     "start": e.start or "", "end": e.end or "", "notes": "",
                                     "bullets": [strip_bullet(f.text) for f in facts]})
        elif e.section == "education":
            degree, school = e.title, e.employer
            details = [strip_bullet(f.text) for f in facts][:4]
            location = ""
            if not school and details:
                # "Example State University, Springfield, MA, USA": the school, then where.
                school, _, location = details.pop(0).partition(",")
                school, location = school.strip(), location.strip()
            data["education"].append({"id": _id(), "degree": degree, "field": "", "school": school,
                                      "location": location, "start": e.start or "", "end": e.end or "",
                                      "details": details})
    # Education written without dates on its own lines: one entry per degree-looking line.
    if not data["education"]:
        lines = [model.fact(i).text for i in model.sections.get("education", []) if model.fact(i)]
        for line in lines:
            if re.search(r"\b(bachelor|master|b\.?\s?tech|m\.?\s?tech|b\.?s\.?|m\.?s\.?|ph\.?d|diploma|degree|mba)\b",
                         line, re.I):
                data["education"].append({"id": _id(), "degree": line, "field": "", "school": "", "location": "",
                                          "start": "", "end": "", "details": []})
            elif data["education"] and not data["education"][-1]["school"]:
                data["education"][-1]["school"] = line
    for i in model.sections.get("certifications", []):
        f = model.fact(i)
        if f and f.kind in ("bullet", "text", "entry"):
            data["certifications"].append({"id": _id(), "name": strip_bullet(f.text), "issuer": "", "date": ""})
    return {"data": clean(data), "warnings": list(model.warnings)}


def _is_list(text: str) -> bool:
    """"AWS, Azure, GCP" -- short items with commas -- rather than a sentence."""
    from ml.resume.health import ACTION_VERBS
    items = [i for i in re.split(r"[,;|]", text) if i.strip()]
    if len(items) < 3 or sum(len(i.split()) for i in items) / len(items) > 4:
        return False
    first = (re.match(r"[A-Za-z-]+", text.strip()) or [""])[0].lower()
    # "Architected pipelines via GitHub Actions, ..." and "Familiar with Harness, ..." are sentences.
    sentence = (first in ACTION_VERBS or first.endswith("ed") or first in _SENTENCE_OPENERS
                or len(items[0].split()) > 4)
    return not sentence


_SENTENCE_OPENERS = {"hands-on", "working", "familiar", "expertise", "proficient", "skilled", "experience",
                     "strong", "knowledge", "experienced", "expert", "deep", "solid", "extensive"}


def _headed_lines(extracted) -> list[tuple[str, str, list[str]]]:
    """(heading as written, section kind, the lines under it) for each section, in order."""
    from ml.tailoring.structure import _is_heading
    out: list[tuple[str, str, list[str]]] = []
    for block in extracted.blocks:
        if block.source in ("header", "footer"):
            continue
        kind = _is_heading(block)
        if kind:
            out.append((block.text.strip().rstrip(":"), kind, []))
        elif out and block.text.strip():
            out[-1][2].append(block.text.strip())
    return out


# -- the built resume as the master --------------------------------------------------------

def make_master(resume_dir: Path) -> tuple[Path, Path | None]:
    """Copy the built .docx in as the master resume; the one it replaces is kept in data/builder/replaced/.

    Returns (the new master, where the old one was kept).
    """
    if not DOCX.exists():
        raise FileNotFoundError("Make the resume first (press Preview).")
    resume_dir.mkdir(parents=True, exist_ok=True)
    kept = None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for old in resume_dir.glob("base_resume.*"):
        BACKUPS.mkdir(parents=True, exist_ok=True)
        kept = BACKUPS / f"{stamp}-{old.name}"
        shutil.move(str(old), kept)
    target = resume_dir / "base_resume.docx"
    shutil.copy2(DOCX, target)
    return target, kept
