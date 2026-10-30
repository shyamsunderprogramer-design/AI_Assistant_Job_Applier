"""No test reaches past this Mac (conftest._nothing_leaves_this_mac): not another host, not the
local model servers or the running app, not an AI command-line tool."""

import socket
import subprocess

import pytest


def test_another_host_is_out_of_reach():
    with pytest.raises(OSError):
        socket.create_connection(("example.com", 443), timeout=2)
    with pytest.raises(OSError):
        socket.create_connection(("93.184.215.14", 443), timeout=2)


@pytest.mark.parametrize("port", [11434, 20128, 8770])
def test_the_model_servers_and_the_running_app_are_out_of_reach(port):
    with pytest.raises(ConnectionRefusedError, match="may not connect"):
        socket.create_connection(("127.0.0.1", port), timeout=2)


@pytest.mark.parametrize("command", [["claude", "-p", "hi"], ["/Users/someone/.local/bin/claude", "-p"],
                                     ["ollama", "list"], ["gh", "release", "list"], "claude -p hi"])
def test_ai_tools_cannot_be_started(command):
    with pytest.raises(PermissionError, match="may not run"):
        subprocess.run(command, shell=isinstance(command, str), capture_output=True)


def test_ordinary_commands_still_run():
    assert subprocess.run(["echo", "fine"], capture_output=True, text=True).stdout.strip() == "fine"


def test_a_requests_call_to_the_web_fails_fast():
    import requests
    with pytest.raises(requests.RequestException):
        requests.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs", timeout=2)
