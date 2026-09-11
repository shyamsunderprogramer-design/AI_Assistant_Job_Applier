"""Tailoring without the API — bring your own model.

The API path (`resume/tailor.py`) costs roughly $0.14 a job, which is not worth
it when the person already pays for a Claude subscription, or wants to use a
local model, or simply will not spend money on this. The work Claude does there
is a single prompt and a single JSON reply, so nothing about it actually
requires the API.

So this module splits that one call into two halves a human can carry across:

    main.py brief <job-id>      -> writes a prompt file to paste anywhere
    main.py accept <job-id>     -> reads the JSON reply back, guards it,
                                   and writes the same ATS-safe .docx

Everything after the model call is identical to the API path — the same
fabrication guard (README.md §C8), the same writer, the same review note. The
guard matters MORE here, not less: output pasted in from a chat window has had
no programmatic check at all before it arrives.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from resume.parser import Resume
from resume.tailor import SYSTEM_PROMPT, TailorResult, build_prompt
from resume.guard import check_no_fabrication

log = logging.getLogger(__name__)

BRIEF_HEADER = """\
================================================================================
TAILORING BRIEF — {company}: {title}
================================================================================

HOW TO USE THIS FILE

  1. Copy EVERYTHING below the line marked "COPY FROM HERE".
  2. Paste it into Claude (claude.ai, or the Claude Code session you are
     already paying for), ChatGPT, or any local model.
  3. Save the JSON reply to a file.
  4. Run:  python main.py accept {job_id} --file <that file>

The reply is checked for fabrication before anything is written, exactly as the
API path is. A reply that invents a skill, metric, employer or date is rejected,
not quietly used — so a model that embellishes cannot put words in your resume.

  Job     : {title}
  Company : {company}
  Job id  : {job_id}
  Apply   : {url}

================================================================================
COPY FROM HERE
================================================================================

{system}

--------------------------------------------------------------------------------

{user}
"""


def write_brief(
    resume: Resume,
    job_id: int,
    company: str,
    title: str,
    jd_text: str,
    url: str,
    path: Path,
) -> Path:
    """Write a self-contained prompt file for one job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        BRIEF_HEADER.format(
            job_id=job_id,
            company=company,
            title=title,
            url=url or "(none)",
            system=SYSTEM_PROMPT,
            user=build_prompt(resume, title, company, jd_text),
        ),
        encoding="utf-8",
    )
    return path


def parse_reply(text: str) -> dict:
    """Pull the JSON object out of a pasted model reply.

    Hand-carried text arrives with whatever the chat window wrapped around it —
    a code fence, a "Here's your tailored resume:" preamble, a trailing
    question. Be liberal about all of that, and strict about the contents.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("The reply file is empty.")

    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(
            "No JSON object found. Save the model's reply verbatim — it should "
            "start with '{' and contain \"bullets\"."
        )
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"The reply is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("The reply must be a JSON object, not a list or a string.")
    if "bullets" not in payload:
        raise ValueError(
            'The reply has no "bullets" key. It is probably prose rather than '
            "the JSON the brief asked for — re-run the prompt and keep the JSON."
        )
    return payload


def result_from_reply(resume: Resume, text: str) -> TailorResult:
    """Build a guarded TailorResult from a pasted reply.

    The guard runs here exactly as it does on the API path. Output that came
    from a chat window has had no programmatic check before this point, so this
    is the only thing standing between an embellishing model and a resume that
    claims something untrue.
    """
    payload = parse_reply(text)

    result = TailorResult(
        summary=payload.get("summary"),
        bullets=[b for b in payload.get("bullets", []) if isinstance(b, dict)],
        skills_order=[str(s) for s in payload.get("skills_order", [])],
        omitted=[str(s) for s in payload.get("omitted", [])],
        gaps=[str(s) for s in payload.get("gaps", [])],
        raw_response=text,
    )
    result.guard = check_no_fabrication(resume.text(), result.tailored_text())
    if not result.guard.ok:
        log.warning("Fabrication guard rejected a pasted tailoring:\n%s", result.guard.report())
    return result
