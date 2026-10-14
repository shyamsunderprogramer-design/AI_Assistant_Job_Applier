"""A skills section read in one pass: one heading, one line per group, the tools and nothing else.

A master resume's skills arrive in every shape: "Cloud: AWS, Azure" rows; a group's label over its own
bullets ("Security, DevSecOps & Compliance:", then "Snyk – integrated the CLI into ..."); two groups run
together on one line ("... GCP (GKE, Cloud Build) Containerization & Orchestration: Docker ..."); and a
tail on every row saying what was done with them (" – Used for SLOs and root cause analysis"). Printed
as they came, a tailored resume had two Skills sections, a paragraph of semicolons, and the same
"tools – what I did" pattern on every line (Oct 2026).

Here each group becomes "Label: tool, tool, tool":
  * a label line and the items under it are one group;
  * groups run together on one line are split apart;
  * an item's own description is cut at its dash ("Snyk – integrated ..." is "Snyk"), and a long
    description in brackets goes with it ("Python (API integrations with Jira, ...)" is "Python");
  * "Vulcan familiarity – exposure to ..." says what it says: "Vulcan (familiar)";
  * a sentence among the tools ("Ensured HIPAA/FedRAMP readiness by ...") keeps only the standards it
    names (HIPAA, FedRAMP): the claim itself belongs in a job's bullets, where it already is;
  * a tool named in two groups is kept once: a language in the languages group, anything else where
    it first appears.
A group written as sentences rather than a list ("Infrastructure as Code: Built Terraform modules
for ...") is kept as written. Nothing is added that the resume does not say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A row's own description starts at a spaced dash: "Jenkins, Bamboo – Implemented blue-green ...".
_TAIL = re.compile(r"\s+[–—]\s+|\s+-\s+(?=[a-z])")
# Two groups on one line: "... GCP (GKE, Cloud Build) Containerization & Orchestration: Docker ...".
_JOINED = re.compile(r"(?<=[)\w.;])\s+([A-Z][A-Za-z0-9&/+.,' -]{2,60}?):\s")
_STANDARDS = re.compile(r"\b(HIPAA|FedRAMP|SOC ?2|PCI(?:[- ]DSS)?|CIS|NIST(?: 800-\d+)?|ISO ?27001|GDPR|HITRUST|"
                        r"FISMA|CMMC|STIG|SOX)\b")
_FAMILIAR = re.compile(r"^(.*?)\s+(familiarity|exposure|basics|awareness)$|^(?:familiar with|exposure to)\s+(.*)$", re.I)
LANGUAGES = {"python", "bash", "powershell", "groovy", "java", "javascript", "typescript", "go", "golang", "ruby",
             "perl", "c", "c++", "c#", "scala", "kotlin", "rust", "sql", "shell", "php", "r", "swift", "dart",
             "lua", "haskell", "elixir", "erlang", "clojure", "matlab"}
_FUNCTION_WORDS = {"with", "for", "to", "in", "of", "and", "via", "using", "into", "across", "on"}


@dataclass
class Group:
    label: str
    items: list[str] = field(default_factory=list)   # the tools, once cleaned
    raw: list[str] = field(default_factory=list)     # as written
    prose: bool = False                              # sentences, kept as written


def _verbish(word: str) -> bool:
    from ml.resume.health import ACTION_VERBS
    w = word.lower().strip(",.;:")
    return w in ACTION_VERBS or (w.endswith("ed") and len(w) > 4)


def cut_tail(text: str) -> str:
    """The tools before the row's own description: "Jenkins, Bamboo – Implemented ..." -> "Jenkins, Bamboo"."""
    depth = 0
    for m in _TAIL.finditer(text):
        before = text[:m.start()]
        depth = before.count("(") - before.count(")")
        if depth <= 0:
            return before.strip()
    return text.strip()


def split_items(text: str, at: str = ",;") -> list[str]:
    """Items split at commas and semicolons, but not inside brackets: "Azure (AKS, Key Vault), GCP"."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch in at and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [i.strip(" .\t") for i in out if i.strip(" .\t")]


def _phrase(piece: str) -> bool:
    """Words joined by "with", "across", "for"...: written, not a tool's name (brackets aside)."""
    words = re.sub(r"\([^()]*\)", " ", piece).split()
    return len(words) >= 4 and bool(_FUNCTION_WORDS & {w.lower() for w in words})


def is_sentence(text: str) -> bool:
    words = text.split()
    if not words:
        return False
    return _verbish(words[0]) or (len(words) > 6 and "," not in text and "(" not in text)


def tidy(item: str) -> str:
    """One tool as a skills line shows it."""
    item = re.sub(r"\s+", " ", item).strip(" .:-–—")
    # A bracket that describes rather than lists: "(API integrations with Jira, SonarQube Cloud, ...)".
    def bracket(m):
        inside = m.group(1)
        first = inside.split(",")[0].split()
        return "" if len(first) > 2 and _FUNCTION_WORDS & {w.lower() for w in first} else m.group(0)
    item = re.sub(r"\s*\(([^()]*)\)", bracket, item).strip()
    m = _FAMILIAR.match(item)
    if m:
        tool = (m.group(1) or m.group(3) or "").strip()
        if tool:
            item = f"{tool} (familiar)"
    return item


def base(item: str) -> str:
    """What a tool is called, without its brackets: "Helm (v3)" and "Helm (Kubernetes packages)" are Helm."""
    return re.sub(r"\s*\(.*?\)", "", item).strip().lower()


def split_joined(label: str, rest: str) -> list[tuple[str, str]]:
    """One row that holds two groups, as two: [(label, items), (label, items)]."""
    out = []
    while True:
        m = None
        for found in _JOINED.finditer(rest):
            before = rest[:found.start()]
            if before.count("(") == before.count(")") and len(found.group(1).split()) <= 6:
                m = found
                break
        if m is None:
            out.append((label, rest.strip()))
            return out
        out.append((label, rest[:m.start()].strip()))
        label, rest = m.group(1).strip(), rest[m.end():]


def _row(text: str) -> tuple[str, str] | None:
    """("Label", "items") for "Label: items" with a short label, else None."""
    label, sep, rest = text.partition(":")
    if not sep or not 1 <= len(label.split()) <= 7 or label.strip().lower().startswith("http"):
        return None
    return label.strip(" -•*\t"), rest.strip()


def groups(lines: list[str]) -> list[Group]:
    """The groups a skills section's lines make, as written (not yet cleaned)."""
    out: list[Group] = []
    open_group: Group | None = None          # a label line, collecting the items under it
    for line in lines:
        text = re.sub(r"^[\s•*·▪◦-]+", "", line or "").strip()
        if not text:
            continue
        row = _row(text)
        if row and not row[1]:
            open_group = Group(row[0])
            out.append(open_group)
            continue
        if row and not is_sentence(row[1]) or row and open_group is None:
            open_group = None
            for label, rest in split_joined(*row):
                out.append(Group(label, raw=[rest]))
            continue
        if open_group is not None:
            open_group.raw.append(text)
        elif out and not out[-1].label:
            out[-1].raw.append(text)
        else:
            out.append(Group("", raw=[text]))
    return out


def clean(found: list[Group]) -> list[Group]:
    """Each group's tools, cut from their descriptions, each tool kept once across the section."""
    for g in found:
        tools: list[str] = []
        # "Snyk – integrated ...; SonarQube Cloud – configured ...": each item's description is its own.
        parts = [(raw, cut_tail(part)) for raw in g.raw for part in split_items(raw, ";")]
        texts = [text for _, text in parts]
        pieces = [p for t in texts if not is_sentence(t) for p in split_items(t)]
        # A list names tools; "NoSQL and streaming with DynamoDB" or "macOS and UNIX across build
        # estates" is a phrase someone wrote, and the group is kept as they wrote it.
        listy = bool(pieces) and not any(_phrase(p) for p in pieces) and not (
            len(g.raw) == 1 and is_sentence(texts[0]))
        if not listy:
            g.prose = True
            g.items = [" ".join(r.strip() for r in g.raw)]
            continue
        for raw, text in parts:
            if is_sentence(text):
                tools += [s for s in _STANDARDS.findall(raw) if s]
                continue
            tools += [tidy(p) for p in split_items(text)]
        seen, kept = set(), []
        for t in tools:
            if t and base(t) not in seen:
                seen.add(base(t))
                kept.append(t)
        g.items = kept
    home = next((g for g in found if not g.prose and re.search(r"language|programming|scripting", g.label, re.I)), None)
    languages = {base(i) for i in home.items if base(i) in LANGUAGES} if home else set()
    seen: set[str] = set()
    for g in found:
        if g.prose:
            continue
        kept = []
        for item in g.items:
            b = base(item)
            if g is not home and b in languages:
                continue                     # Python is shown with the languages, not under Automation
            if b in seen:
                continue
            seen.add(b)
            kept.append(item)
        g.items = kept
    return [g for g in found if g.items]


def lines_of(found: list[Group]) -> list[str]:
    """Each group as its one line."""
    out = []
    for g in found:
        body = g.items[0] if g.prose else ", ".join(g.items)
        out.append(f"{g.label}: {body}" if g.label else body)
    return out


def compact(lines: list[str]) -> list[str]:
    """A skills section's lines, as the clean lines it should print."""
    return lines_of(clean(groups(lines)))
