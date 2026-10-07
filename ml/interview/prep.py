"""Interview preparation for a job the person has applied to.

One model call reads the posting and the person's resume and returns:
likely questions, scenario questions with STAR outlines drawn only from the
resume, topics to study, questions this company tends to ask for this kind
of role, strengths and weaknesses for the role, and a chance-for-this-role
estimate (labelled as one).

Links are never written by the model -- a made-up URL is worse than none.
The app builds them: the official documentation for tools it knows, and
YouTube and web searches for everything else, plus the company's own site
and careers page where the company register has them. Glassdoor and the like
are linked as searches, never scraped.

Kept in data/interview/<job id>.json; the links-first PDF goes in the job's
application folder.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

FOLDER = PROJECT_ROOT / "data" / "interview"
SHOWN_FOR = ("Applied", "Interviewing")

SYSTEM = """You prepare a candidate for a job interview. You get the job posting and the
candidate's resume. Reply with JSON only, in this shape:

{"likely_questions": [{"q": "...", "why": "what in the posting makes it likely"}],
 "scenarios": [{"q": "a behavioural or situational question",
                "star": {"situation": "...", "task": "...", "action": "...", "result": "..."},
                "from": "the resume line this answer is built on, quoted"}],
 "topics": [{"topic": "short name of a technology or concept", "why": "why to study it for this role"}],
 "company_questions": [{"q": "a question companies like this one commonly ask for this role"}],
 "strengths": ["..."], "weaknesses": ["..."],
 "chance": {"rating": "Strong | Good | Fair | Long shot", "why": "one or two sentences"}}

Rules:
- 8 to 12 likely questions; 5 to 7 scenarios; 6 to 10 topics; 5 to 8 company questions;
  3 to 5 strengths; 2 to 4 weaknesses.
- STAR answers use ONLY facts from the resume. Never invent employers, projects, numbers
  or results. If the resume gives no result for something, say what was delivered instead.
- Weaknesses are gaps between the posting and the resume, each with one honest way to address
  it in the interview.
- The chance rating is an estimate from how well the resume covers the posting, not a promise.
- No links or URLs anywhere: the app adds its own."""

# Official documentation for tools the app knows; anything else gets search links.
DOCS = {
    "kubernetes": "https://kubernetes.io/docs/home/", "terraform": "https://developer.hashicorp.com/terraform/docs",
    "docker": "https://docs.docker.com/", "aws": "https://docs.aws.amazon.com/", "azure": "https://learn.microsoft.com/azure/",
    "gcp": "https://cloud.google.com/docs", "google cloud": "https://cloud.google.com/docs",
    "helm": "https://helm.sh/docs/", "argo cd": "https://argo-cd.readthedocs.io/", "argocd": "https://argo-cd.readthedocs.io/",
    "ansible": "https://docs.ansible.com/", "jenkins": "https://www.jenkins.io/doc/",
    "github actions": "https://docs.github.com/actions", "gitlab": "https://docs.gitlab.com/ee/ci/",
    "prometheus": "https://prometheus.io/docs/", "grafana": "https://grafana.com/docs/", "datadog": "https://docs.datadoghq.com/",
    "vault": "https://developer.hashicorp.com/vault/docs", "kafka": "https://kafka.apache.org/documentation/",
    "python": "https://docs.python.org/3/", "go": "https://go.dev/doc/", "golang": "https://go.dev/doc/",
    "rust": "https://doc.rust-lang.org/book/", "java": "https://docs.oracle.com/en/java/", "linux": "https://www.kernel.org/doc/html/latest/",
    "postgresql": "https://www.postgresql.org/docs/", "postgres": "https://www.postgresql.org/docs/",
    "openshift": "https://docs.openshift.com/", "istio": "https://istio.io/latest/docs/", "elk": "https://www.elastic.co/guide/",
    "elasticsearch": "https://www.elastic.co/guide/", "snowflake": "https://docs.snowflake.com/", "spark": "https://spark.apache.org/docs/latest/",
    "sre": "https://sre.google/books/", "site reliability engineering": "https://sre.google/books/",
    "observability": "https://opentelemetry.io/docs/", "opentelemetry": "https://opentelemetry.io/docs/",
    "ci/cd": "https://docs.github.com/actions", "gitops": "https://opengitops.dev/",
}


def _path(job_id: int) -> Path:
    return FOLDER / f"{job_id}.json"


def links_for(topic: str) -> list[dict]:
    """Official docs where known, then a video search and a web search -- never a guessed URL."""
    key = topic.strip().lower()
    out = []
    doc = DOCS.get(key) or next((url for name, url in DOCS.items() if re.search(rf"\b{re.escape(name)}\b", key)), None)
    if doc:
        out.append({"label": "Official docs", "url": doc})
    out.append({"label": "Video tutorials", "url": f"https://www.youtube.com/results?search_query={quote_plus(topic + ' tutorial')}"})
    out.append({"label": "Interview questions", "url": f"https://www.google.com/search?q={quote_plus(topic + ' interview questions')}"})
    return out


def company_links(company: str, title: str) -> list[dict]:
    """The company's own pages where the register has them, then searches."""
    links = []
    try:
        from data_engineering.companies.db import normalize_name
        with sqlite3.connect(f"file:{PROJECT_ROOT / 'data' / 'companies.db'}?mode=ro", uri=True) as db:
            row = db.execute("SELECT website, careers_url FROM companies WHERE name_normalized = ? LIMIT 1",
                             (normalize_name(company),)).fetchone()
        if row and row[0]:
            links.append({"label": "Company website", "url": row[0] if row[0].startswith("http") else "https://" + row[0]})
        if row and row[1]:
            links.append({"label": "Careers page", "url": row[1]})
    except Exception:
        pass
    q = quote_plus
    links += [
        {"label": "Interview experiences (Glassdoor search)", "url": f"https://www.google.com/search?q={q(company + ' ' + title + ' interview questions site:glassdoor.com')}"},
        {"label": "Interview process (web search)", "url": f"https://www.google.com/search?q={q(company + ' interview process ' + title)}"},
        {"label": "Company on LinkedIn", "url": f"https://www.linkedin.com/search/results/companies/?keywords={q(company)}"},
        {"label": "Engineering blog (search)", "url": f"https://www.google.com/search?q={q(company + ' engineering blog')}"},
    ]
    return links


def load(job_id: int) -> dict | None:
    try:
        return json.loads(_path(job_id).read_text())
    except (OSError, ValueError):
        return None


def prepare(cfg, job) -> dict:
    """Make (or remake) the prep for one job, with links added by the app."""
    from ml.resume.llm import complete
    from ml.resume.pipeline import load_base_resume
    resume = load_base_resume(cfg).text()
    user = (f"JOB: {job.title} at {job.company}\n\nPOSTING:\n{(job.description or '')[:12000]}\n\n"
            f"CANDIDATE RESUME:\n{resume[:14000]}")
    reply = complete(SYSTEM, user, cfg, max_tokens=8000).text
    try:
        body = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
    except ValueError as exc:
        raise RuntimeError("The model did not return the prep in the expected shape; press again.") from exc
    # Every number in a STAR answer must be on the resume; anything else is flagged, not hidden.
    on_resume = set(re.findall(r"\d+(?:\.\d+)?", resume))
    for s in body.get("scenarios") or []:
        told = " ".join(str(v) for v in (s.get("star") or {}).values())
        new_numbers = sorted({n for n in re.findall(r"\d+(?:\.\d+)?", told) if n not in on_resume})
        if new_numbers:
            s["check"] = f"These numbers are not on your resume: {', '.join(new_numbers)} — check before using."
    for item in body.get("topics") or []:
        item["links"] = links_for(str(item.get("topic") or ""))
    body["company_links"] = company_links(job.company, job.title)
    body["made"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body["company"], body["title"], body["job_id"] = job.company, job.title, job.id
    FOLDER.mkdir(parents=True, exist_ok=True)
    _path(job.id).write_text(json.dumps(body, indent=1))
    return body


def write_pdf(prep: dict, target: Path) -> Path:
    """The links-first PDF: an index of clickable links and the question list, nothing long."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    def esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def link(item):
        return f'<a href="{esc(item["url"])}" color="#0b5cad"><u>{esc(item["label"])}</u></a>'

    styles = getSampleStyleSheet()
    body = []
    body.append(Paragraph(f"Interview prep — {esc(prep['title'])} at {esc(prep['company'])}", styles["Title"]))
    body.append(Paragraph("An index of links. The full prep — answers, outlines and reasons — is on the job page.",
                          styles["Italic"]))
    body.append(Spacer(1, 0.15 * inch))
    body.append(Paragraph("Company", styles["Heading2"]))
    body.append(Paragraph(" · ".join(link(l) for l in prep.get("company_links") or []), styles["BodyText"]))
    body.append(Paragraph("Topics to study", styles["Heading2"]))
    for t in prep.get("topics") or []:
        body.append(Paragraph(f"<b>{esc(t.get('topic'))}</b> — " + " · ".join(link(l) for l in t.get("links") or []),
                              styles["BodyText"]))
    body.append(Paragraph("Questions to practise", styles["Heading2"]))
    for q in (prep.get("likely_questions") or []) + (prep.get("company_questions") or []):
        body.append(Paragraph("• " + esc(q.get("q")), styles["BodyText"]))
    for s in prep.get("scenarios") or []:
        body.append(Paragraph("• " + esc(s.get("q")) + " <i>(STAR outline on the job page)</i>", styles["BodyText"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(target), pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch).build(body)
    return target
