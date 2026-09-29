"""The chat: Claude Code, headless, with the job tools and nothing else.

Runs `claude -p` -- the person's own Claude Code sign-in, so their plan and
no API key -- as a subprocess per message, resuming one conversation. It is
boxed in by its flags, not by asking nicely:

  --tools ""              no built-in tools: no shell, no files, no web
  --strict-mcp-config     no MCP servers but ours
  --mcp-config            ours: backend/assistant/mcp_server.py
  --allowedTools          the job tools, minus confirm_action -- confirming is
                          the Confirm button's
  --permission-mode dontAsk   anything not allowed is refused, not asked about

and it runs in data/assistant/, so no project notes or settings of this
repository reach it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from backend.config.loader import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "data" / "assistant"
SESSION_FILE = STATE_DIR / "session.json"
TURN_TIMEOUT_S = 600

ASK_TOOLS = ["search_jobs", "job_detail", "tailoring_review", "pipeline_status",
             "recent_applications"]
PROPOSE_TOOLS = ["propose_tailor", "propose_apply", "propose_remember_answer",
                 "propose_set_status"]

SYSTEM_PROMPT = """You are the assistant inside the person's job-search app. You help them \
find and weigh jobs, check their tailored resumes, and get applications out.

Use the tools for every fact about their jobs, resumes and applications -- never guess a \
number, a company or a status. Keep answers short: a small table or a few bullets, \
plain words, job ids included so they can ask about one.

To change anything, use a propose_* tool. Proposing does nothing by itself: a Confirm \
button appears under your reply and the person decides. Say what you proposed and that it \
waits for them. Never claim something was done unless a tool said so.

Applications: they sign in to employer accounts and press Submit themselves; never offer \
to do either, and never ask for a password. Only save an answer to a screening question \
when the person told you that answer."""


@dataclass
class Turn:
    id: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    error: str | None = None


_turns: dict[str, Turn] = {}
_busy = threading.Lock()


def claude_path() -> str | None:
    from ml.resume.llm import claude_code_path
    return claude_code_path()


def mcp_config() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"jobs": {
        "command": sys.executable,
        "args": ["-m", "backend.assistant.mcp_server"],
        "env": {"PYTHONPATH": str(PROJECT_ROOT)},
    }}}, indent=1))
    return path


def command(message: str, session: str | None, new: bool, model: str | None = None) -> list[str]:
    allowed = [f"mcp__jobs__{name}" for name in ASK_TOOLS + PROPOSE_TOOLS]
    cmd = [claude_path() or "claude", "-p", message,
           "--output-format", "stream-json", "--verbose",
           "--tools", "",
           "--strict-mcp-config", "--mcp-config", str(mcp_config()),
           "--allowedTools", ",".join(allowed),
           "--permission-mode", "dontAsk",
           "--append-system-prompt", SYSTEM_PROMPT]
    if session:
        cmd += ["--session-id", session] if new else ["--resume", session]
    if model:
        cmd += ["--model", model]
    return cmd


def environment() -> dict[str, str]:
    """No API key: the chat is the person's sign-in's (ml/resume/llm.py)."""
    from ml.resume.llm import claude_code_env
    return claude_code_env()


def _session() -> tuple[str, bool]:
    """(conversation id, whether it is new)."""
    try:
        data = json.loads(SESSION_FILE.read_text())
        if data.get("id") and data.get("started"):
            return data["id"], False
    except (OSError, ValueError):
        pass
    return str(uuid.uuid4()), True


def _remember_session(session: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"id": session, "started": True}))


def new_conversation() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def parse(line: str) -> list[dict]:
    """One stream-json line -> the events the page shows."""
    try:
        data = json.loads(line)
    except ValueError:
        return []
    kind = data.get("type")
    if kind == "assistant":
        out = []
        for block in (data.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                out.append({"type": "tool", "name": block.get("name", "").replace("mcp__jobs__", "")})
        return out
    if kind == "user":
        out = []
        for block in (data.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            try:
                result = json.loads(text)
                # The MCP SDK wraps a tool's string in {"result": "<json>"}.
                if isinstance(result, dict) and isinstance(result.get("result"), str):
                    result = json.loads(result["result"])
            except ValueError:
                continue
            if isinstance(result, dict) and result.get("pending_action"):
                out.append({"type": "action", "id": result["pending_action"],
                            "summary": result.get("will_do", "")})
        return out
    if kind == "result":
        return [{"type": "done", "session": data.get("session_id"),
                 "error": data.get("is_error") and (data.get("result") or "failed")}]
    return []


def start(message: str, model: str | None = None, runner=subprocess.Popen) -> Turn:
    """Send one message; the reply streams into the returned Turn."""
    turn = Turn(uuid.uuid4().hex[:12])
    _turns[turn.id] = turn
    if claude_path() is None:
        turn.error, turn.done = "Claude Code is not installed (the `claude` command was not found).", True
        return turn
    threading.Thread(target=_run, args=(turn, message, model, runner), daemon=True).start()
    return turn


def _run(turn: Turn, message: str, model: str | None, runner) -> None:
    with _busy:                        # one conversation, one message at a time
        session, new = _session()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            process = runner(command(message, session, new, model), cwd=STATE_DIR, env=environment(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            turn.error, turn.done = f"Could not start Claude Code: {exc}", True
            return
        deadline = time.monotonic() + TURN_TIMEOUT_S
        for line in process.stdout:
            for event in parse(line):
                if event["type"] == "done":
                    if event.get("session"):
                        _remember_session(event["session"])
                    if event.get("error"):
                        turn.error = str(event["error"])[:500]
                else:
                    turn.events.append(event)
            if time.monotonic() > deadline:
                process.kill()
                turn.error = "No answer in 10 minutes — stopped."
                break
        process.wait()
        if process.returncode and not turn.error and not turn.events:
            err = (process.stderr.read() if process.stderr else "")[-400:]
            turn.error = _plain_error(err) or f"Claude Code stopped (exit {process.returncode})."
        turn.done = True


def _plain_error(stderr: str) -> str:
    low = stderr.lower()
    if "login" in low or "not logged in" in low or "authenticate" in low:
        return "Claude Code is not signed in. Run `claude` in a terminal once and sign in."
    if "usage limit" in low or "rate limit" in low:
        return "Your Claude plan's usage limit is reached for now — try again later."
    return stderr.strip()


def poll(turn_id: str, since: int = 0) -> dict:
    turn = _turns.get(turn_id)
    if turn is None:
        return {"error": "unknown turn", "done": True, "events": []}
    return {"events": turn.events[since:], "next": len(turn.events), "done": turn.done,
            "error": turn.error}
