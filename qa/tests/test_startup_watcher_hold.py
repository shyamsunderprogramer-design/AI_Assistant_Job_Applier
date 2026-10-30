"""Exercise startup without live services, timers, threads, config or database access."""

import runpy
import threading
from unittest.mock import Mock

import pytest

from backend.api import app as webapp, morning_pages
from backend.apply import execution, inbox
from backend.core.startup import HOLD_ENV
from qa.tests.test_execution_queue import db  # noqa: F401


@pytest.fixture
def startup(monkeypatch):
    monkeypatch.delenv(HOLD_ENV, raising=False)
    services = {"config": Mock(), "mail": Mock(return_value={}),
                "morning": Mock(return_value=False), "recover": Mock(),
                "main": Mock(return_value=0)}
    monkeypatch.setattr(webapp, "cfg", services["config"])
    monkeypatch.setattr(webapp, "main", services["main"])
    monkeypatch.setattr(execution, "recover", services["recover"])
    monkeypatch.setattr(inbox, "check", services["mail"])
    monkeypatch.setattr(morning_pages, "catch_up", services["morning"])
    threads = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.target, self.name, self.daemon = target, name, daemon
            self.started = False
            threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, "Thread", Thread)
    return services, threads


def boot():
    with pytest.raises(SystemExit) as result:
        runpy.run_module("backend.api", run_name="__main__")
    assert result.value.code == 0


def test_entrypoint_hold_creates_no_threads_or_delayed_work(startup, monkeypatch, caplog):
    services, threads = startup
    monkeypatch.setenv(HOLD_ENV, "1")
    sleep = Mock(side_effect=AssertionError("a held watcher must not sleep"))
    monkeypatch.setattr(inbox.time, "sleep", sleep)
    boot()
    assert threads == []
    sleep.assert_not_called()
    services["mail"].assert_not_called()
    services["morning"].assert_not_called()
    # Hold only watchers; ordinary startup and history recovery still run.
    services["config"].assert_called_once_with()
    services["recover"].assert_called_once_with()
    services["main"].assert_called_once_with()
    assert "inbox-watch held" in caplog.text
    assert "morning-catch-up held" in caplog.text
    # Removing the environment value cannot awaken a timer: none was created.
    monkeypatch.delenv(HOLD_ENV)
    assert threads == []


@pytest.mark.parametrize("value", [None, "0"])
def test_normal_entrypoint_starts_both_watchers(startup, monkeypatch, value):
    services, threads = startup
    if value is not None:
        monkeypatch.setenv(HOLD_ENV, value)
    boot()
    assert [t.name for t in threads] == ["inbox-watch", "morning-catch-up"]
    assert all(t.started and t.daemon for t in threads)
    services["mail"].assert_not_called()
    services["morning"].assert_not_called()
    services["recover"].assert_called_once_with()


def test_direct_starters_are_also_held(startup, monkeypatch):
    services, threads = startup
    monkeypatch.setenv(HOLD_ENV, "1")
    for _ in range(3):
        assert inbox.watch(services["config"], first_after_s=0) is None
        assert morning_pages.watch_mornings(every_s=0) is None
    assert threads == []
    for service in services.values():
        service.assert_not_called()


@pytest.mark.parametrize("kind,initial,interval", [("mail", 90, 1800), ("morning", 60, 900)])
def test_normal_watchers_keep_existing_schedule_and_action(startup, monkeypatch, kind, initial, interval):
    services, threads = startup
    if kind == "mail":
        assert inbox.watch(services["config"]) is threads[0]
    else:
        morning_pages.watch_mornings()
    sleeps = []

    class Done(BaseException):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            services[kind].assert_not_called()
        else:
            raise Done()

    monkeypatch.setattr(inbox.time, "sleep", sleep)
    with pytest.raises(Done):
        threads[0].target()  # no real thread or waiting; exactly one mocked iteration
    assert sleeps == [initial, interval]
    services[kind].assert_called_once_with()
    if kind == "mail":
        services["config"].assert_called_once_with()
    else:
        services["config"].assert_not_called()


def test_new_start_after_explicit_hold_removal_preserves_normal_behavior(startup, monkeypatch):
    services, threads = startup
    monkeypatch.setenv(HOLD_ENV, "1")
    inbox.watch(services["config"])
    morning_pages.watch_mornings()
    assert threads == []
    monkeypatch.delenv(HOLD_ENV)
    inbox.watch(services["config"])
    morning_pages.watch_mornings()
    assert [t.name for t in threads] == ["inbox-watch", "morning-catch-up"]


def test_held_entrypoint_recovers_outbox_without_schema_changes(db, monkeypatch):
    from backend.apply import runner
    from data_engineering.db import session as session_mod
    from qa.tests.test_history_outbox import lines, pending, rows

    monkeypatch.setenv(HOLD_ENV, "1")
    monkeypatch.setattr(webapp, "cfg", lambda: db[0])
    monkeypatch.setattr(webapp, "main", lambda: 0)
    def forbidden(*args, **kwargs):
        raise AssertionError("held startup attempted a watcher or thread")
    monkeypatch.setattr(threading, "Thread", forbidden)
    monkeypatch.setattr(inbox, "check", forbidden)
    monkeypatch.setattr(morning_pages, "catch_up", forbidden)
    pending(monkeypatch)  # fictional application committed; export deliberately failed
    event_id, payload, exported = rows()[0]
    assert exported is None

    def schema():
        with session_mod._engine.connect() as conn:
            return conn.exec_driver_sql(
                "select type,name,sql from sqlite_master order by type,name").all()

    before = schema()
    boot()
    boot()  # startup recovery must remain idempotent while watchers are held
    assert schema() == before
    assert lines() == [payload]
    assert rows()[0][0] == event_id and rows()[0][2] is not None
    assert runner.AUDIT_LOG.is_relative_to(db[2])


def test_database_guards_refuse_the_project_database():
    import sqlite3
    from data_engineering.db.session import init_engine
    from qa.tests.conftest import REAL_ROOT

    path = REAL_ROOT / "data/jobs.db"
    with pytest.raises(PermissionError, match="own database"):
        init_engine(f"sqlite:///{path}")
    with pytest.raises(PermissionError, match="own database"):
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
