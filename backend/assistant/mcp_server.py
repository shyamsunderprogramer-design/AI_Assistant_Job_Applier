"""The job tools as an MCP server, for Claude Code.

Two ways in, one set of tools:

  * The web app's chat runs `claude -p` with every built-in tool off and this
    server as the only one (backend/assistant/chat.py). There,
    `confirm_action` is withheld: the Confirm button is the person's.
  * `.mcp.json` in the project folder offers it to Claude Code opened there,
    where Claude Code asks the person before each tool call anyway.

Run: python -m backend.assistant.mcp_server  (stdio)
"""

from __future__ import annotations

import json
import urllib.request

from mcp.server.mcpserver import MCPServer

from backend.assistant import tools

WEB_APP = "http://127.0.0.1:8770"

server = MCPServer(
    "jobs",
    instructions=(
        "The person's job search: postings found for them, match scores against "
        "their resume, tailored resumes, applications and the runs that feed them. "
        "Tools named propose_* never act: they record a pending action the person "
        "confirms or cancels. Say plainly what was proposed and that nothing has "
        "happened yet."),
)


@server.tool()
def search_jobs(text: str = "", board: str = "", min_match: int = 0, posted_within_days: int = 0,
                status: str = "", open_only: bool = True, limit: int = 15) -> str:
    """Find jobs. `text` matches title, company or location words; `board` is the
    source (greenhouse, workday, lever, ashby, adzuna, ...); `min_match` is a
    percentage; `status` is e.g. "Not Applied". Best match first."""
    return _json(tools.search_jobs(text, board, min_match, posted_within_days, status, open_only, limit))


@server.tool()
def job_detail(job_id: int) -> str:
    """One job: company, title, match, age, salary, experience asked, the start of
    the description, and which resume files exist for it."""
    return _json(tools.job_detail(job_id))


@server.tool()
def tailoring_review(job_id: int) -> str:
    """The review note of the job's tailored resume: gaps against the posting,
    each rewritten line before and after, and what was left out."""
    return _json(tools.tailoring_review(job_id))


@server.tool()
def pipeline_status() -> str:
    """Open jobs by status, applications today, and progress of every long run
    (discovery probe, scraping, enrichment)."""
    return _json(tools.pipeline_status())


@server.tool()
def recent_applications(limit: int = 10) -> str:
    """Applications recorded as submitted, newest first."""
    return _json(tools.recent_applications(limit))


@server.tool()
def propose_tailor(job_id: int) -> str:
    """Propose tailoring the resume for one job. Does nothing until confirmed."""
    return _json(tools.propose_tailor(job_id))


@server.tool()
def propose_apply(job_id: int = 0, count: int = 0) -> str:
    """Propose applying: to one job (job_id), or to the next `count` (max 10) in
    the queue. The browser opens; the person signs in and presses Submit. Does
    nothing until confirmed."""
    return _json(tools.propose_apply(job_id, count))


@server.tool()
def propose_remember_answer(question: str, answer: str) -> str:
    """Propose saving the person's answer to a screening question, reused on
    future forms. Only what the person said -- never an inferred answer.
    Does nothing until confirmed."""
    return _json(tools.propose_remember_answer(question, answer))


@server.tool()
def propose_set_status(job_id: int, status: str) -> str:
    """Propose changing a job's status: Not Applied, Manual Review, Applied,
    Interviewing, Offer, Rejected, Closed, or Skipped (not interested). Does
    nothing until confirmed."""
    return _json(tools.propose_set_status(job_id, status))


@server.tool()
def confirm_action(action_id: str) -> str:
    """Carry out a proposed action. Only when the person has just said yes to
    that action in this conversation."""
    request = urllib.request.Request(f"{WEB_APP}/assistant/action/{action_id}/confirm",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        return _json({"error": f"The web app did not take it ({exc}). Is it running on {WEB_APP}?"})


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


if __name__ == "__main__":
    server.run("stdio")
