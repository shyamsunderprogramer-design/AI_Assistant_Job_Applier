"""The chat assistant: boxed in by its flags, and unable to act without a Confirm."""

import json

import pytest

from backend.assistant import chat, tools


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tools, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(chat, "STATE_DIR", tmp_path)
    monkeypatch.setattr(chat, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(tools, "_brief", lambda job_id: {"id": int(job_id), "company": "Acme", "title": "SRE"})
    return tmp_path


def test_claude_runs_with_no_tools_but_the_job_tools(state):
    cmd = chat.command("hi", "abc", new=True)
    assert cmd[cmd.index("--tools") + 1] == ""                       # no shell, files, web
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert all(a.startswith("mcp__jobs__") for a in allowed)
    assert "mcp__jobs__confirm_action" not in allowed                # the button is the person's
    assert cmd[cmd.index("--session-id") + 1] == "abc"
    assert "--resume" in chat.command("again", "abc", new=False)
    servers = json.loads((state / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["jobs"]


def test_the_stream_becomes_text_tool_and_action_events():
    wrapped = json.dumps({"result": json.dumps({"pending_action": "a1", "will_do": "Tailor for Acme"})})
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__jobs__propose_tailor"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": wrapped}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Proposed."}]}},
        {"type": "result", "session_id": "s1", "is_error": False},
    ]
    events = [e for line in lines for e in chat.parse(json.dumps(line))]
    assert events == [{"type": "tool", "name": "propose_tailor"},
                      {"type": "action", "id": "a1", "summary": "Tailor for Acme"},
                      {"type": "text", "text": "Proposed."},
                      {"type": "done", "session": "s1", "error": False}]


def test_a_turn_streams_and_remembers_the_conversation(state, monkeypatch):
    class Fake:
        def __init__(self, cmd, **kw):
            self.returncode, self.stderr = 0, None
            self.stdout = iter([
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}) + "\n",
                json.dumps({"type": "result", "session_id": "sess-9", "is_error": False}) + "\n"])
        def wait(self): return 0
        def kill(self): pass

    monkeypatch.setattr(chat, "claude_path", lambda: "/bin/claude")
    turn = chat.start("hi", runner=Fake)
    for _ in range(100):
        if turn.done:
            break
        __import__("time").sleep(0.02)
    assert [e["text"] for e in turn.events] == ["Hello"] and turn.error is None
    assert chat._session() == ("sess-9", False)


def test_proposing_does_nothing_and_confirming_happens_once(state):
    proposed = tools.propose_set_status(5, "Skipped")
    assert "Nothing has happened" in proposed["next"]
    [pending] = tools.pending_actions()
    assert pending["params"] == {"job_id": 5, "status": "Skipped"}
    assert tools.take(pending["id"], "confirmed")["kind"] == "status"
    assert tools.take(pending["id"], "confirmed") is None            # never twice
    assert tools.pending_actions() == []


def test_a_status_that_does_not_exist_is_refused(state):
    assert "error" in tools.propose_set_status(5, "Not Interested")
    assert tools.pending_actions() == []


def test_a_remembered_answer_is_saved_as_said(state, tmp_path, monkeypatch):
    from backend.apply import answers
    bank = tmp_path / "answers.yaml"
    monkeypatch.setattr(answers, "BANK_PATH", bank)
    tools.propose_remember_answer("What is your notice period?*", "2 weeks")
    [action] = tools.pending_actions()
    tools.take(action["id"], "confirmed")
    tools.run_direct(action)
    assert answers.load(bank) == {"what is your notice period": "2 weeks"}


def test_confirm_route_runs_an_action_once(state, monkeypatch):
    from backend.api import app as webapp
    tools.propose_apply(job_id=7)
    [action] = tools.pending_actions()
    started = []
    monkeypatch.setattr(webapp, "start_task", lambda task, args: (started.append(args),
                        webapp.jsonify(ok=True, started=True))[1])
    client = webapp.app.test_client()
    first = client.post(f"/assistant/action/{action['id']}/confirm").get_json()
    second = client.post(f"/assistant/action/{action['id']}/confirm")
    assert first["ok"] and started == [["apply", "--job-id", "7"]]
    assert second.status_code == 409 and len(started) == 1


def test_an_action_that_cannot_start_waits_again(state, monkeypatch):
    from backend.api import app as webapp
    tools.propose_tailor(3)
    [action] = tools.pending_actions()
    monkeypatch.setattr(webapp, "start_task", lambda task, args: webapp.jsonify(ok=True, started=False))
    reply = webapp.app.test_client().post(f"/assistant/action/{action['id']}/confirm")
    assert reply.status_code == 409 and [a["id"] for a in tools.pending_actions()] == [action["id"]]


def test_the_chat_uses_the_sign_in_never_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = chat.environment()
    assert "ANTHROPIC_API_KEY" not in env and env["PATH"] == "/usr/bin"
