"""The board finder lets go of finished connections after every batch (30 Oct 2026).

From 13 to 29 Oct it ran out of the files a process may hold open. A failed request leaves its
connection pool in a reference cycle that only Python's full garbage collection frees, and a run
holding a million-name probe cache gets one so rarely that 231 closed connections piled up; every
check for the internet failed from then on ("No internet" for 18 hours, with the internet up).
Made-up names, a made-up discoverer and a made-up cycle: no network, no database."""

import gc
import os
import socket
import warnings
import weakref
from types import SimpleNamespace

import pytest

from devops import probe_runner as pr


def test_a_connection_kept_only_by_garbage_is_closed():
    gc.disable()                 # as rarely as a long run gets a full collection: here, never
    try:
        pool, error = SimpleNamespace(), SimpleNamespace()     # a pool and the error that points back at it
        pool.error, error.pool = error, pool
        pool.connection = socket.socket()
        fd, held = pool.connection.fileno(), weakref.ref(pool.connection)
        del pool, error
        os.fstat(fd)             # still open: nothing but the cycle holds it
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            pr.let_go_of_dead_connections()
        assert held() is None
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        gc.enable()


def test_the_run_lets_go_after_every_batch(monkeypatch, tmp_path):
    names = tmp_path / "names.txt"
    names.write_text("\n".join(f"Made-up Company {i}" for i in range(100)) + "\n")
    for attr in ("STATE", "PIDFILE", "LOG", "STOPFILE"):
        monkeypatch.setattr(pr, attr, tmp_path / attr.lower())
    monkeypatch.setattr(pr, "hold_awake", lambda: None)
    monkeypatch.setattr(pr, "online", lambda host="": True)
    monkeypatch.setattr(pr.signal, "signal", lambda *args: None)      # pytest keeps its own handlers
    events = []
    monkeypatch.setattr(pr, "let_go_of_dead_connections", lambda: events.append("let go"))

    class Discoverer:
        def __init__(self, **kwargs):
            self.consumed = 0

        def discover_from_names(self, chunk, sources, max_slugs=0, should_stop=None, on_progress=None):
            self.consumed = len(chunk)
            events.append(f"batch of {len(chunk)}")
            return []

    class Config:
        user_agent, database_url = "made-up", "sqlite://"

        def get(self, key, default=None):
            return default

    from backend.config import loader
    from data_engineering.db import session
    from data_engineering.scraper import discovery, runner
    monkeypatch.setattr(loader, "load_config", lambda: Config())
    monkeypatch.setattr(session, "init_engine", lambda url: None)
    monkeypatch.setattr(discovery, "CompanyDiscoverer", Discoverer)
    monkeypatch.setattr(runner, "build_scrapers", lambda cfg, client: {})

    assert pr.run(names, batch=40) == 0
    assert events == ["batch of 40", "let go", "batch of 40", "let go", "batch of 20", "let go"]
    assert pr.read_state()["status"] == "finished"
