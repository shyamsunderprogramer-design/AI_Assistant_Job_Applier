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


def score_resume(
    resume_text: str,
    jd_text: str,
    requirements: str | None = None,
    company: str | None = None,
    top_n: int = 40,
) -> ScoreResult:
    """Weighted coverage of the JD's salient terms by the resume."""
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
    # missing is already weight-ordered, so the biggest gaps come first.
    return ScoreResult(
        score=round(score, 4),
        matched=matched,
        missing=missing,
        jd_terms=len(weights),
    )
