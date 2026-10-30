"""Your written answers -- the experience questions you answered on applications -- kept for the next ones.

A short answer ("Yes", "3.9", "Non-citizen allowed to work for any employer") is reused word for word
(answers.py). A written one is not: "Describe the deepest AWS estate you have owned" comes back from
the next employer in other words, about another stack, and an answer sent to two employers unread
names the wrong company sooner or later. So a written answer is kept here with the question it
answered, the company and the day, and when a later form asks something like it:

  * the earlier answer is given to the drafter (essays.py) as material -- your own facts and
    phrasing, to build on rather than start over -- never pasted as it was;
  * it is listed on the Questions page, to read and copy from.

Answers come in by themselves: whatever long answer you typed on a form the app opened is kept when
you submit, close the tab, or run out of time. Kept in data/written_answers.json, which is yours.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from backend.apply.filesafe import one_writer, write_whole
from backend.config.loader import PROJECT_ROOT

PATH = PROJECT_ROOT / "data" / "written_answers.json"
MIN_CHARS = 150                       # a written answer, not a fact

_STOP = set("""a an and are as at be by can describe did do does for from have how i in is it me of on or our
please share tell that the this to us walk was we were what when where which who why will with would you your
yourself experience example examples role roles position company team about any give provide briefly ve ll re
s t d m into there their them they one some most more such than then these those also just like all""".split())

# The same thing in an employer's other words: "estate" or "environment", "IAM" or "identity".
_SAME = {
    "estate": "environment", "environments": "environment", "infrastructure": "environment", "account": "accounts",
    "iam": "identity", "access": "identity", "sso": "identity", "permission": "identity", "permissions": "identity",
    "disaster": "dr", "recovery": "dr", "failover": "dr", "resilience": "dr", "continuity": "dr",
    "regions": "region", "multi-region": "region", "cross-region": "region",
    "largest": "scale", "biggest": "scale", "deepest": "scale", "size": "scale",
    "designed": "design", "designing": "design", "architected": "design", "architecture": "design",
    "built": "build", "building": "build", "owned": "own", "ownership": "own",
    "managed": "manage", "managing": "manage", "management": "manage", "handled": "manage", "handle": "manage",
    "changes": "change", "deployment": "change", "deployments": "change", "release": "change", "releases": "change",
    "pipelines": "pipeline", "ci/cd": "pipeline", "cicd": "pipeline",
    "incidents": "incident", "outage": "incident", "outages": "incident", "on-call": "incident",
    "kubernetes": "k8s", "eks": "k8s", "aks": "k8s", "gke": "k8s",
}


def _stem(word: str) -> str:
    for end in ("ing", "ed", "es", "s"):
        if len(word) > len(end) + 3 and word.endswith(end):
            return word[: -len(end)]
    return word


def _words(text: str) -> set[str]:
    out = set()
    for raw in re.findall(r"[a-z0-9][a-z0-9+#/-]*", (text or "").lower()):
        for word in [raw, *raw.split("-")] if "-" in raw else [raw]:
            word = _SAME.get(word, word)
            if word in _STOP or len(word) < 2:
                continue
            out.add(_SAME.get(_stem(word), _stem(word)) if word not in _SAME.values() else word)
    return out


_LETTER = re.compile(r"\s*(dear|hello|hi|greetings|to whom)\b", re.I)


def _norm(text: str) -> str:
    """Words only: "Describe … owned. *" and "describe … owned" are one question."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _id(question: str, answer: str) -> str:
    norm = lambda s: " ".join((s or "").lower().split())
    return hashlib.sha1(f"{norm(question)}\n{norm(answer)}".encode()).hexdigest()[:12]


def load() -> list[dict]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("question") and d.get("answer")]


def _save(items: list[dict]) -> None:
    write_whole(PATH, json.dumps(items, indent=1, ensure_ascii=False))


def is_written(question: str, answer: str) -> bool:
    """An experience question answered in your own words -- not a cover letter, not a fact."""
    q = (question or "").lower()
    if "cover letter" in q or q.strip() in ("additional information", "anything else"):
        return False
    if _LETTER.match(answer or ""):       # a letter typed into a "Message" box is a letter, not an answer
        return False
    return len((answer or "").strip()) >= MIN_CHARS and len(q) >= 20


def keep(question: str, answer: str, company: str = "", title: str = "", job_id: int | None = None,
         when: str | None = None) -> bool:
    """Keep one written answer. False when it is kept already: the same answer to the same question,
    its label starred, spaced or cut short (some forms' labels are kept to 60 characters) included --
    and where a cut-short question was kept, the whole one takes its place."""
    question, answer = " ".join(question.split()), answer.strip()
    asked, said = _norm(question), _norm(answer)
    with one_writer(PATH):                         # any process: read, changed, written back as one
        items = load()
        for item in items:
            if _norm(item["answer"]) != said:
                continue
            then = _norm(item["question"])
            if then.startswith(asked):
                return False
            if asked.startswith(then):
                item["question"] = question
                _save(items)
                return False
        items.append({"id": _id(question, answer), "question": question, "answer": answer, "company": company,
                      "title": title, "job_id": job_id,
                      "date": when or datetime.now(timezone.utc).isoformat(timespec="seconds")})
        _save(items)
    return True


def keep_from(answers: dict, company: str = "", title: str = "", job_id: int | None = None) -> list[str]:
    """Keep the written answers among a form's answers. Returns the questions kept."""
    kept = []
    for question, answer in (answers or {}).items():
        if isinstance(answer, str) and is_written(str(question), answer) \
                and keep(str(question), answer, company, title, job_id):
            kept.append(str(question))
    return kept


def remove(entry_id: str) -> bool:
    with one_writer(PATH):
        items = load()
        left = [i for i in items if i["id"] != entry_id]
        if len(left) == len(items):
            return False
        _save(left)
    return True


def similar(question: str, limit: int = 2, least: float = 0.4) -> list[dict]:
    """Your earlier answers to questions like this one, closest first: how much of the new question
    the earlier question covers, with what the earlier answer talks about counting half."""
    asked = _words(question)
    if len(asked) < 2:
        return []
    scored = []
    for item in load():
        then, said = _words(item["question"]), _words(item["answer"])
        score = (len(asked & then) + 0.5 * len((asked & said) - then)) / len(asked)
        if score >= least:
            scored.append((score, item))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [{**item, "score": round(score, 2)} for score, item in scored[:limit]]
