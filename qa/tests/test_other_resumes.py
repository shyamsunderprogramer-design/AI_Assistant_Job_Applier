"""Other people's resumes, laid out as well as the owner's: the rules are global.

Two fixtures in other fields and formats -- a frontend developer who lists
Education first and a comma list of skills, and a data analyst with an
Objective, "Work History" and "Label: items" skill rows.
"""

from pathlib import Path

import pytest

from ml.resume import writer
from ml.resume.parser import parse_text
from ml.resume.tailor import TailorResult

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def own_contact_line(monkeypatch):
    monkeypatch.setattr(writer, "applicant_contact_line", lambda: None)


def plan(name):
    base = parse_text((FIXTURES / f"resume_{name}.txt").read_text())
    return writer.plan_document(base, TailorResult(summary=None))


@pytest.mark.parametrize("name,person", [("frontend", "ALEX RIVERA"), ("analyst", "PRIYA NAIR")])
def test_a_name_in_capitals_is_the_header_not_a_section(name, person):
    blocks = plan(name)
    assert blocks[0] == ("name", person) and blocks[1][0] == "contact"
    assert ("heading", person) not in blocks


@pytest.mark.parametrize("name", ["frontend", "analyst"])
def test_education_comes_last_and_experience_before_it(name):
    headings = [t.lower() for k, t in plan(name) if k == "heading"]
    assert "education" in headings[-1]
    assert any(w in " ".join(headings[:-1]) for w in ("experience", "work history"))


def test_every_labelled_skill_row_is_kept():
    rows = [t for k, t in plan("analyst") if k == "skill"]
    assert [r.split(":")[0] for r in rows] == ["Languages", "BI & Visualisation", "Data", "Methods"]


def test_a_plain_skill_list_and_certificates_are_text():
    blocks = plan("frontend")
    assert any(k == "text" and t.startswith("React, TypeScript") for k, t in blocks)
    assert ("text", "AWS Certified Cloud Practitioner (2023)") in blocks
