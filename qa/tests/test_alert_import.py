"""The alert mailbox keeps going when Gmail stops answering for a moment (data_engineering/scraper/alert_import.py)."""

import pytest

from data_engineering.scraper import alert_import


class FakeImap:
    """Answers each call from a script of (call, result) pairs; an exception in place of a result is raised."""

    script: list = []

    def __init__(self, host, timeout=None):
        self._next("connect")

    def _next(self, call):
        expected, result = FakeImap.script.pop(0)
        assert expected == call, (expected, call)
        if isinstance(result, Exception):
            raise result
        return result

    def login(self, user, password):
        return self._next("login")

    def select(self, box, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        return self._next(command)

    def logout(self):
        pass


@pytest.fixture
def imap(monkeypatch):
    monkeypatch.setattr(alert_import.imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return FakeImap


def test_a_reconnect_that_times_out_is_tried_again(imap):
    # 21 Oct 2026: a fetch timed out, the reconnect's login timed out too, and the alerts stage failed.
    imap.script = [("connect", None), ("login", ("OK", [])), ("FETCH", TimeoutError("read timed out")),
                   ("connect", None), ("login", TimeoutError("read timed out")),
                   ("connect", None), ("login", ("OK", [])), ("FETCH", ("OK", [(b"1 (BODY[] {3}", b"raw")]))]
    box = alert_import._Mailbox("me@example.com", "app password")
    assert box.fetch(b"1") == b"raw"
    assert imap.script == []


def test_it_gives_up_after_three_tries(imap):
    imap.script = [("connect", None), ("login", TimeoutError("read timed out"))] * 3
    box = alert_import._Mailbox("me@example.com", "app password")
    with pytest.raises(TimeoutError):
        box.search('(FROM "indeed.com")')
    assert imap.script == []
