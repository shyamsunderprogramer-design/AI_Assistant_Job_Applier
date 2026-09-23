"""ATS match scoring — keyword overlap between a resume and a JD.

Pure Python, no sklearn: the corpus here is two documents, so a real TF-IDF
buys little over frequency-weighted coverage of the JD's salient terms. The
score answers one question: *of the things this JD asks for, how much does the
resume visibly evidence?*

Returns the matched and missing terms alongside the number, because the missing
list is the actually useful output — it tells you what to emphasise.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

STOPWORDS = {
    "a", "about", "above", "across", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "been", "being", "both", "but", "by", "can", "could", "do", "does", "doing",
    "each", "for", "from", "had", "has", "have", "he", "her", "here", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "just", "like", "may", "me", "might", "more",
    "most", "must", "my", "no", "not", "of", "on", "one", "only", "or", "other", "our", "out",
    "over", "own", "per", "run", "same", "she", "should", "so", "some", "such", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those", "through", "to",
    "too", "under", "up", "us", "use", "used", "using", "very", "via", "was", "we", "well",
    "were", "what", "when", "where", "which", "while", "who", "will", "with", "within",
    "would", "you", "your",
    # JD boilerplate that carries no signal about the actual role
    "ability", "able", "across", "applicant", "apply", "background", "benefits", "candidate",
    "candidates", "career", "company", "compensation", "culture", "employer", "equal",
    "experience", "help", "hiring", "including", "job", "join", "looking", "opportunity",
    "position", "role", "salary", "team", "teams", "us", "work", "working", "years",
    "diverse", "diversity", "inclusion", "employment", "status", "orientation", "gender",
    "race", "religion", "veteran", "disability", "regardless", "consideration", "offer",
    "range", "base", "pay", "bonus", "equity", "location", "remote", "office", "hybrid",
}

# Concrete skills/technologies. These are what an ATS and a human screener
# actually look for, so they carry more weight than generic JD prose — and
# `resume.guard` reuses this list to spot invented skills.
SKILL_TERMS = {
    "python", "java", "javascript", "typescript", "go", "golang", "rust", "ruby", "php",
    "scala", "kotlin", "swift", "c++", "c#", "sql", "nosql", "bash", "matlab", "perl",
    "django", "flask", "fastapi", "spring", "rails", "express", "react", "angular", "vue",
    "svelte", "node", "nodejs", "graphql", "rest", "grpc", "kafka", "rabbitmq", "celery",
    "redis", "postgres", "postgresql", "mysql", "mongodb", "cassandra", "dynamodb",
    "elasticsearch", "snowflake", "databricks", "spark", "hadoop", "airflow", "dbt",
    "kubernetes", "docker", "terraform", "ansible", "jenkins", "circleci", "helm", "istio",
    "aws", "azure", "gcp", "lambda", "s3", "ec2", "eks", "bigquery", "redshift", "vault",
    "pytorch", "tensorflow", "keras", "scikit", "sklearn", "pandas", "numpy", "huggingface",
    "llm", "nlp", "pyspark", "tableau", "looker", "powerbi", "git", "linux", "unix",
    "prometheus", "grafana", "datadog", "splunk", "jira", "nginx", "hive", "presto", "trino",
    "flink", "beam", "pytest", "junit", "selenium", "graphene", "openai", "anthropic",
    "langchain", "kotlin", "scala", "protobuf", "websocket", "oauth", "saml", "kerberos",
    "api", "apis", "backend", "frontend", "fullstack", "devops", "sre", "ci", "cd", "etl",
    "scalability", "latency", "throughput", "sharding", "caching", "observability",
    "architecture", "algorithms", "debugging", "profiling", "mentoring", "leadership",
}

# Multi-word skills worth matching as a unit — "machine learning" shouldn't
# score twice via two weak unigrams.
PHRASES = (
    "machine learning", "deep learning", "data engineering", "data science",
    "distributed systems", "microservices", "unit testing", "integration testing",
    "continuous integration", "continuous deployment", "infrastructure as code",
    "rest api", "graph ql", "message queue", "event driven", "object oriented",
    "test driven", "version control", "code review", "agile", "scrum",
    "natural language processing", "computer vision", "large language model",
    "data pipeline", "etl", "data warehouse", "real time", "high availability",
)


@dataclass
class ScoreResult:
    score: float                       # 0.0 – 1.0
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    jd_terms: int = 0

    @property
    def percent(self) -> int:
        return round(self.score * 100)

    def summary(self) -> str:
        return (
            f"{self.percent}% match — {len(self.matched)}/{self.jd_terms} JD terms covered. "
            f"Top gaps: {', '.join(self.missing[:8]) or 'none'}"
        )


# Below this, a "description" is a few metadata lines rather than a posting.
# Scraped postings average 6,758 characters; a job-alert email gives 217.
DESCRIPTION_FLOOR = 600

# The most a posting can score when there is no description to measure.
#
# The comment below says a title-only score "lands in the twenties". That was
# true of a title that matched poorly; a title that matches WELL lands at
# 100%, because with almost no text every salient term is trivially covered.
# A real example sat at the top of a 2,482-job list: "Embedded Systems
# Engineer", 25 characters of description, scored 1.00 -- above every job that
# had a real description and had actually been measured.
#
# A score is "how much of this posting's salient vocabulary does the resume
# cover". With no posting text that quantity is undefined, and reporting 100%
# claims certainty from no evidence. The cap sits just below the default
# threshold so these surface as "worth a look" rather than "best match", and
# `score_basis` still records why.
TITLE_ONLY_CEILING = 0.55


def score_basis(description: str | None) -> str:
    """"full" if there is a real description to measure against, else "title".

    The distinction matters because the two produce numbers on different
    scales. A posting scored on its title alone lands in the twenties no matter
    how well it fits, which reads as "weak match" when it means "not
    measurable" -- and the difference decides whether someone opens the job or
    scrolls past it.
    """
    return "full" if len((description or "").strip()) >= DESCRIPTION_FLOOR else "title"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall((text or "").lower())


def content_terms(text: str) -> list[str]:
    """Tokens worth scoring: no stopwords, no bare numbers, length >= 2."""
    return [
        t.strip(".-")
        for t in tokenize(text)
        if t not in STOPWORDS and len(t) >= 2 and not t.isdigit()
    ]


def _phrases_present(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {p for p in PHRASES if p in lowered}


def jd_keyword_weights(
    jd_text: str,
    requirements: str | None = None,
    company: str | None = None,
    top_n: int = 40,
) -> dict[str, float]:
    """The JD's salient terms, weighted by frequency (sublinear).

    Two corrections that make the score mean something:

    * General terms come from the *requirements* section when we have one.
      Scoring against the whole JD lets company blurb and benefits boilerplate
      ("backbone", "mission", "equity") dominate terms no resume can match.
    * Concrete skills are weighted well above generic prose, since those are
      what an ATS keyword screen and a human reviewer actually check.
    """
    general_source = requirements if requirements and len(requirements) > 200 else jd_text
    company_tokens = set(tokenize(company or ""))

    counts: dict[str, int] = {}
    for term in content_terms(general_source):
        # 2-char tokens are almost all fragments ("re", "ll") — noise.
        if len(term) < 3 or term in company_tokens:
            continue
        counts[term] = counts.get(term, 0) + 1

    weighted = {term: 1 + math.log(count) for term, count in counts.items()}

    # Skills are matched against the FULL JD (they're often listed outside the
    # requirements block) and outweigh generic prose.
    jd_all = f"{jd_text}\n{requirements or ''}"
    for term in set(content_terms(jd_all)):
        if term in SKILL_TERMS and term not in company_tokens:
            weighted[term] = weighted.get(term, 1.0) * 2.5
    for phrase in _phrases_present(jd_all):
        weighted[phrase] = weighted.get(phrase, 1.0) * 2.5

    ranked = sorted(weighted.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ranked[:top_n])


# How much of the final score survives when the job is not the job this person
# does. Not zero: a data engineer CAN take a platform role, and the tool should
# show it rather than hide it. But it must not outrank their own field.
WRONG_ROLE_FLOOR = 0.45


def _title_words(value) -> str:
    """A job title as space-separated words, padded, for whole-word matching.

    Punctuation becomes a space, because ATS titles are full of it and the
    separator carries no meaning: "Engineer, Data Platform", "Engineer - Cloud
    Infrastructure" and "Engineer (Data Platform)" are all the same job as
    "Data Platform Engineer". Padding both ends lets `" sre " in title` mean
    the word, so "sre" is never found inside "Presenter".
    """
    return " " + " ".join(re.split(r"[^a-z0-9+#]+", str(value or "").lower()) ) + " "


def role_fit(job_title: str, search_titles=(), held_titles=()) -> float:
    """Is this the kind of job this person does? 1.0 yes, 0.0 no.

    The coverage score alone cannot tell. Scoring a data engineer's resume
    against two postings gave 97% for Senior Data Engineer and 94% for Senior
    DevSecOps Engineer -- three points between his own field and someone
    else's -- because both descriptions are full of AWS, Python, Kubernetes,
    Terraform and CI/CD, and coverage counts shared vocabulary.

    Titles are where the difference actually lives, and the person's own
    titles are the most reliable statement of what they do.
    """
    title = _title_words(job_title)
    if not title.strip():
        return 1.0                  # nothing to judge; do not penalise

    for phrase in held_titles:
        phrase = _title_words(phrase).strip()
        if phrase and phrase in title:
            return 1.0              # they have literally held this title

    best = 0.0
    for phrase in search_titles:
        phrase = _title_words(phrase).strip()
        if not phrase:
            continue
        if phrase in title:
            return 1.0              # the search asked for exactly this
        # A partial match: "data engineer" against "senior data platform
        # engineer". Every word present, in any order, is still the same job.
        words = phrase.split()
        if len(words) > 1 and all(f" {w} " in title for w in words):
            best = max(best, 0.85)
    return best


def score_resume(
    resume_text: str,
    jd_text: str,
    requirements: str | None = None,
    company: str | None = None,
    top_n: int = 40,
    job_title: str | None = None,
    search_titles=(),
    held_titles=(),
) -> ScoreResult:
    """How well this posting fits, from both halves of the question.

    Coverage answers "do you have what it asks for". Role fit answers "is this
    your job". Only the first was ever measured, and shared vocabulary made
    every infrastructure-flavoured posting look like a match for anyone with
    an infrastructure-flavoured resume.
    """
    weights = jd_keyword_weights(
        jd_text, requirements=requirements, company=company, top_n=top_n
    )
    if not weights:
        return ScoreResult(score=0.0, jd_terms=0)

    resume_terms = set(content_terms(resume_text)) | _phrases_present(resume_text)

    matched: list[str] = []
    missing: list[str] = []
    hit_weight = 0.0
    total_weight = 0.0

    for term, weight in weights.items():
        total_weight += weight
        if term in resume_terms:
            hit_weight += weight
            matched.append(term)
        else:
            missing.append(term)

    score = hit_weight / total_weight if total_weight else 0.0
    if score_basis(jd_text) == "title":
        score = min(score, TITLE_ONLY_CEILING)

    # Scale by whether this is the person's job at all. A wrong-field posting
    # keeps WRONG_ROLE_FLOOR of its coverage rather than zero: it is still
    # worth seeing, it just must not sit above their own field.
    fit = role_fit(job_title, search_titles, held_titles) if job_title else 1.0
    score *= WRONG_ROLE_FLOOR + (1 - WRONG_ROLE_FLOOR) * fit

    # missing is already weight-ordered, so the biggest gaps come first.
    return ScoreResult(
        score=round(score, 4),
        matched=matched,
        missing=missing,
        jd_terms=len(weights),
    )
