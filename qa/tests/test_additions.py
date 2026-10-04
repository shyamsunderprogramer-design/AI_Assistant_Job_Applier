""""I've used this": the person's own line, merged under their job, never invented."""

import pytest

from ml.resume import additions
from ml.resume.additions import NotAdded
from ml.resume.parser import parse_text

MASTER = """JORDAN EXAMPLE
jordan@example.com

EXPERIENCE
PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present
- Built Terraform modules for 200+ AWS workloads.
- Ran Kubernetes clusters on EKS.
SYSTEMS ENGINEER, GLOBEX, USA     Jan 2016 – Dec 2021
- Maintained VMware clusters.
"""
ROLE = "PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present"


@pytest.fixture
def store(tmp_path):
    return tmp_path / "additions.yaml"


def test_the_jobs_come_from_the_resume():
    assert additions.roles(parse_text(MASTER))[0] == ROLE


def test_degrees_are_not_jobs():
    with_degree = MASTER + "\nEDUCATION\nMaster of Science     Jan 2023 – Dec 2024\n"
    assert all("Master" not in r for r in additions.roles(parse_text(with_degree)))


def test_a_line_is_merged_under_its_job_and_nowhere_else(store):
    additions.add(parse_text(MASTER), ROLE, "Defined Protobuf schemas for service contracts", "Protobuf",
                  True, path=store)
    resume = additions.apply(parse_text(MASTER), path=store)
    lines = [l for s in resume.sections for l in s.lines]
    acme = lines.index(ROLE)
    globex = next(i for i, l in enumerate(lines) if l.startswith("SYSTEMS ENGINEER"))
    assert lines.index("- Defined Protobuf schemas for service contracts.") in range(acme, globex)
    assert "Protobuf" in resume.text()                    # the guard and ATS check see it


@pytest.mark.parametrize("role,line,tool,confirmed,why", [
    (ROLE, "Defined Protobuf schemas for service contracts", "Protobuf", False, "Tick"),
    ("MADE UP JOB, NOWHERE", "Defined Protobuf schemas for service contracts", "Protobuf", True, "Pick"),
    (ROLE, "Used it", "Protobuf", True, "full line"),
    (ROLE, "Defined schemas for all service contracts at scale", "Protobuf", True, "Mention Protobuf"),
])
def test_what_cannot_be_added(store, role, line, tool, confirmed, why):
    with pytest.raises(NotAdded, match=why):
        additions.add(parse_text(MASTER), role, line, tool, confirmed, path=store)
    assert additions.load(store) == []


def test_the_same_line_twice_is_refused_and_it_can_be_removed(store):
    base = parse_text(MASTER)
    additions.add(base, ROLE, "Defined Protobuf schemas for service contracts", "Protobuf", True, path=store)
    with pytest.raises(NotAdded, match="already"):
        additions.add(base, ROLE, "Defined Protobuf schemas for service contracts.", "Protobuf", True, path=store)
    assert additions.remove(ROLE, "Defined Protobuf schemas for service contracts.", path=store)
    assert additions.load(store) == []


def test_the_route_refuses_an_unticked_line(monkeypatch, store):
    from backend.api import app as webapp
    monkeypatch.setattr(additions, "ADDITIONS_PATH", store)
    monkeypatch.setattr("ml.resume.pipeline.load_base_resume", lambda cfg: parse_text(MASTER))
    monkeypatch.setattr(webapp, "cfg", lambda: None)   # the real one loads .env into the environment
    client = webapp.app.test_client()
    bad = client.post("/resume/additions", json={"tool": "Protobuf", "role": ROLE,
                                                 "line": "Defined Protobuf schemas for contracts", "confirmed": False})
    assert bad.status_code == 400 and "Tick" in bad.get_json()["error"]
    good = client.post("/resume/additions", json={"tool": "Protobuf", "role": ROLE,
                                                  "line": "Defined Protobuf schemas for contracts", "confirmed": True})
    assert good.get_json()["ok"] and additions.load(store)[0]["tool"] == "Protobuf"
