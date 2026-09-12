"""One folder per application, holding everything needed to apply to it.

Tailoring already produces files, but they land loose in `resume/output/` with
a shared stem, so preparing to apply means hunting for three filenames. A
packet gathers them under one directory named for the posting.

`job.md` is written first and unconditionally: it needs no model and no API
key, so a packet is useful the moment it exists — even before a resume has
been tailored or a letter drafted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config.loader import PROJECT_ROOT
from jobage import age_label
from jobfields import UNSTATED, experience_label, salary_label, workplace_label
from resume.writer import output_filename

log = logging.getLogger(__name__)

JOB_SUMMARY = "job.md"
RESUME_FILE = "resume.docx"
LETTER_FILE = "cover-letter.md"
REVIEW_FILE = "review.txt"


@dataclass
class Packet:
    """Where a packet is and what it is still missing."""

    path: Path
    job_id: int
    company: str
    title: str
    has_resume: bool = False
    has_letter: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.has_resume and self.has_letter

    def missing(self) -> list[str]:
        gaps = []
        if not self.has_resume:
            gaps.append("tailored resume")
        if not self.has_letter:
            gaps.append("cover letter")
        return gaps


def packet_root(cfg) -> Path:
    root = Path(cfg.get("resume.output_dir", "resume/output"))
    return root if root.is_absolute() else PROJECT_ROOT / root


def packet_dir(cfg, company: str, title: str, external_id: str = "") -> Path:
    """One directory per posting, named the way the resume file already is.

    Sharing `output_filename`'s stem keeps a packet traceable to exactly one
    posting, and means an existing tailored resume can be found beside it.
    """
    stem = output_filename(company, title, external_id)
    return packet_root(cfg) / stem[:-5]     # drop ".docx"


def _line(label: str, value: str) -> str:
    return f"- **{label}:** {value}\n"


def render_job_summary(job) -> str:
    """Everything about the posting worth having open while applying."""
    facts = [
        ("Workplace", workplace_label(job.workplace)),
        ("Experience", experience_label(job.experience_min_years,
                                        job.experience_max_years)),
        ("Salary", salary_label(job.salary_min, job.salary_max,
                                job.salary_currency, job.salary_period)),
        ("Posted", age_label(job) or UNSTATED),
        ("Location", job.location or UNSTATED),
        ("Board", job.source),
    ]

    out = [f"# {job.title}\n", f"## {job.company}\n\n"]
    if job.application_url:
        out.append(f"**[Apply]({job.application_url})**\n\n")
    for label, value in facts:
        out.append(_line(label, value))
    out.append(f"\n<!-- job id {job.id} · scored "
               f"{(job.ats_match_score or 0) * 100:.0f}% -->\n")

    if job.requirements:
        out.append("\n## What they ask for\n\n")
        out.append(job.requirements.strip() + "\n")

    # The full description last: useful to have, too long to lead with.
    if job.description:
        out.append("\n## Full posting\n\n")
        out.append(job.description.strip() + "\n")
    return "".join(out)


def build_packet(cfg, job, letter_text: str | None = None) -> Packet:
    """Create or refresh the packet folder for one job.

    Never deletes: a letter drafted earlier survives a rebuild, so re-running
    this to pick up a newly tailored resume cannot cost you work.
    """
    directory = packet_dir(cfg, job.company, job.title, job.external_id or "")
    directory.mkdir(parents=True, exist_ok=True)

    (directory / JOB_SUMMARY).write_text(render_job_summary(job), encoding="utf-8")

    if letter_text:
        (directory / LETTER_FILE).write_text(letter_text, encoding="utf-8")

    packet = Packet(
        path=directory,
        job_id=job.id,
        company=job.company,
        title=job.title,
        has_resume=(directory / RESUME_FILE).exists(),
        has_letter=(directory / LETTER_FILE).exists(),
    )

    # A resume tailored before packets existed sits beside the folder under the
    # flat naming. Adopt it rather than making the user tailor again.
    if not packet.has_resume:
        legacy = packet_root(cfg) / output_filename(
            job.company, job.title, job.external_id or ""
        )
        if legacy.exists():
            (directory / RESUME_FILE).write_bytes(legacy.read_bytes())
            packet.has_resume = True
            packet.notes.append(f"adopted the existing tailored resume ({legacy.name})")
            review = legacy.with_name(legacy.stem + "_review.txt")
            if review.exists():
                (directory / REVIEW_FILE).write_text(
                    review.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
                )
    return packet
