"""Shared test setup.

Evidence from the form filler and the application log are written to real
folders by default. Tests must never add to them: before this fixture existed
the filler tests had left 93 fake "runs" among the real ones.
"""

import pytest


@pytest.fixture(autouse=True)
def _keep_apply_output_out_of_the_repo(tmp_path, monkeypatch):
    from backend.apply import greenhouse, runner

    monkeypatch.setattr(greenhouse, "RUNS_DIR", tmp_path / "apply-runs")
    monkeypatch.setattr(runner, "AUDIT_LOG", tmp_path / "applications.jsonl")


@pytest.fixture(autouse=True)
def _no_real_applicant_in_resumes(monkeypatch):
    """Resume tests must not pick up the contact details of whoever runs them."""
    from ml.resume import writer

    monkeypatch.setattr(writer, "applicant_contact_line", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_answer_bank(tmp_path, monkeypatch):
    """The person's learned answers must not leak into, or be written by, tests."""
    from backend.apply import answers

    monkeypatch.setattr(answers, "BANK_PATH", tmp_path / "answers.yaml")
