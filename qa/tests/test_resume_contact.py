"""The resume's contact line comes from the Applicant page, not the old resume.

The form and the resume reach the same recruiter; when they disagree, the
resume's number is the one that gets dialled.
"""

from ml.resume import writer
from ml.resume.writer import _base_header, _is_contact_line
from backend.apply.profile import Applicant


class Section:
    def __init__(self, heading, lines):
        self.heading, self.lines = heading, lines


class Base:
    def __init__(self, header):
        self.sections = [Section("HEADER", header)]


OLD = ["Jane Doe", "WA, USA (Open to Relocate) | +1 (978) 555-0000 | old@example.com | LinkedIn",
       "Senior DevOps Engineer | Terraform"]


def with_profile(monkeypatch, **fields):
    person = Applicant(personal={"first_name": "Jane", "last_name": "D",
                                 "email": "new@example.com", "phone": "+1 512 555 0199"},
                       location={"city": "Austin", "state": "TX"},
                       links={"linkedin": "https://www.linkedin.com/in/jane/?isSelfProfile=true"})
    for key, value in fields.items():
        setattr(person, key, value)
    monkeypatch.undo()                      # drop the conftest stub for this test
    monkeypatch.setattr("backend.apply.profile.load", lambda path=None: person)


def test_the_contact_line_is_replaced_by_the_profile(monkeypatch):
    with_profile(monkeypatch)
    header = _base_header(Base(OLD))
    assert header[1] == ("Austin, TX (Open to Relocate) · +1 512 555 0199 · "
                         "new@example.com · linkedin.com/in/jane")


def test_the_name_and_headline_are_the_resumes_own(monkeypatch):
    """The profile's short last name must not replace the resume's full one."""
    with_profile(monkeypatch)
    header = _base_header(Base(OLD))
    assert header[0] == "Jane Doe" and header[2] == OLD[2]


def test_open_to_relocate_is_only_kept_when_the_resume_said_it(monkeypatch):
    with_profile(monkeypatch)
    header = _base_header(Base(["Jane Doe", "WA | old@example.com"]))
    assert "Relocate" not in header[1]


def test_no_profile_keeps_the_resume_exactly(monkeypatch):
    monkeypatch.setattr(writer, "applicant_contact_line", lambda: None)
    assert _base_header(Base(OLD)) == OLD


def test_contact_lines_are_recognised():
    assert _is_contact_line(OLD[1])
    assert _is_contact_line("Austin · +1 512 555 0199")
    assert not _is_contact_line(OLD[2])
