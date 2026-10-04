"""The job page: a teaser is not tailored, a pasted posting replaces it, and the bar follows the steps."""

import pytest

from ml.resume import progress


def test_rounds_share_the_bar_and_each_step_moves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "FOLDER", tmp_path)
    progress.begin(7, rounds=2)
    seen = [progress.read(7)["pct"]]
    for r in range(2):
        progress.next_round(r)
        for name in ("writing", "fitting", "saving", "checking"):
            progress.step(name)
            seen.append(progress.read(7)["pct"])
    assert seen == sorted(seen) and seen[-1] < 100
    assert "Round 2 of 2" in progress.read(7)["label"]
    assert progress.read(7)["upto"] > progress.read(7)["pct"]
    progress.finish()
    assert progress.read(7)["pct"] == 100 and progress.read(7)["done"]


def test_steps_outside_a_rewrite_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "FOLDER", tmp_path)
    progress.finish()
    progress.step("writing")
    assert not list(tmp_path.iterdir())


@pytest.fixture
def client(monkeypatch):
    # No real settings: loading them puts the person's .env into the
    # environment, and later tests then call real models.
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: None)
    return webapp, webapp.app.test_client()


def test_a_teaser_is_not_sent_to_the_writer(client, monkeypatch):
    webapp, c = client
    monkeypatch.setattr(webapp, "_teaser_words", lambda job_id: 40)
    monkeypatch.setattr(webapp, "run_command", lambda *a: pytest.fail("the writer must not run"))
    reply = c.post("/job/1/align")
    assert reply.status_code == 400 and "40-word teaser" in reply.get_json()["error"]


def test_a_short_paste_is_refused(client):
    _, c = client
    reply = c.post("/job/1/posting", json={"text": "Senior engineer wanted."})
    assert reply.status_code == 400 and "too short" in reply.get_json()["error"]
