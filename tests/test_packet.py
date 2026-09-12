"""Application packet tests — offline, temp directories, no DB.

A packet is the folder someone opens when they sit down to apply, so the things
worth testing are that it is useful while still incomplete, and that rebuilding
it never costs work already done.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from resume.packet import (
    JOB_SUMMARY,
    LETTER_FILE,
    RESUME_FILE,
    build_packet,
    packet_dir,
    render_job_summary,
)


@dataclass
class FakeJob:
    id: int = 178
    company: str = "ClickUp"
    title: str = "GTM DevOps Engineer"
    external_id: str = "ec1b5de9"
    source: str = "ashby"
    location: str = "United States · Remote"
    description: str = "We are building the everything app."
    requirements: str = "- 4+ years of DevOps\n- Kubernetes"
    application_url: str = "https://jobs.ashbyhq.com/clickup/ec1b5de9/application"
    ats_match_score: float = 0.91
    workplace: str | None = "remote"
    experience_min_years: int | None = 4
    experience_max_years: int | None = None
    salary_min: int | None = 160000
    salary_max: int | None = 210000
    salary_currency: str | None = "USD"
    salary_period: str | None = "year"
    posted_at: datetime | None = datetime(2026, 6, 9, tzinfo=timezone.utc)
    found_at: datetime | None = datetime(2026, 9, 1, tzinfo=timezone.utc)


class Cfg:
    def __init__(self, root):
        self.root = str(root)

    def get(self, key, default=None):
        return self.root if key == "resume.output_dir" else default


# -- the summary a person reads while applying ------------------------------

def test_the_summary_leads_with_the_job_and_the_link():
    text = render_job_summary(FakeJob())

    assert text.startswith("# GTM DevOps Engineer")
    assert "ClickUp" in text
    assert "https://jobs.ashbyhq.com/clickup/ec1b5de9/application" in text


def test_the_summary_carries_the_parsed_facts():
    text = render_job_summary(FakeJob())

    assert "remote" in text
    assert "4+ yrs" in text
    assert "$160K–$210K" in text


def test_an_unstated_fact_says_so_rather_than_guessing():
    text = render_job_summary(FakeJob(salary_min=None, salary_max=None, workplace=None))

    assert "not published" in text


def test_the_requirements_come_before_the_full_posting():
    text = render_job_summary(FakeJob())

    assert text.index("What they ask for") < text.index("Full posting")


# -- the folder -------------------------------------------------------------

def test_a_packet_is_useful_before_anything_is_tailored(tmp_path):
    packet = build_packet(Cfg(tmp_path), FakeJob())

    assert (packet.path / JOB_SUMMARY).exists()
    assert not packet.complete
    assert packet.missing() == ["tailored resume", "cover letter"]


def test_the_folder_is_named_for_the_posting(tmp_path):
    directory = packet_dir(Cfg(tmp_path), "ClickUp", "GTM DevOps Engineer", "ec1b5de9")

    assert directory.name == "clickup_gtm-devops-engineer_ec1b5de9"
    assert not directory.name.endswith(".docx")


def test_a_letter_is_written_when_given(tmp_path):
    packet = build_packet(Cfg(tmp_path), FakeJob(), letter_text="Dear Hiring Manager,\n")

    assert (packet.path / LETTER_FILE).read_text() == "Dear Hiring Manager,\n"
    assert packet.has_letter


def test_rebuilding_never_destroys_a_letter_already_drafted(tmp_path):
    cfg = Cfg(tmp_path)
    build_packet(cfg, FakeJob(), letter_text="the good letter\n")

    again = build_packet(cfg, FakeJob())      # no letter passed this time

    assert (again.path / LETTER_FILE).read_text() == "the good letter\n"
    assert again.has_letter


def test_the_summary_is_refreshed_on_a_rebuild(tmp_path):
    cfg = Cfg(tmp_path)
    build_packet(cfg, FakeJob(salary_min=None, salary_max=None))

    again = build_packet(cfg, FakeJob())      # salary published since

    assert "$160K–$210K" in (again.path / JOB_SUMMARY).read_text()


def test_a_resume_tailored_before_packets_existed_is_adopted(tmp_path):
    # The flat naming predates packets; making someone tailor again to get a
    # folder would waste the work and, on the API path, the money.
    cfg = Cfg(tmp_path)
    legacy = tmp_path / "clickup_gtm-devops-engineer_ec1b5de9.docx"
    legacy.write_bytes(b"PK\x03\x04 pretend docx")
    (tmp_path / "clickup_gtm-devops-engineer_ec1b5de9_review.txt").write_text(
        "rewrote three bullets", encoding="utf-8"
    )

    packet = build_packet(cfg, FakeJob())

    assert packet.has_resume
    assert (packet.path / RESUME_FILE).read_bytes() == b"PK\x03\x04 pretend docx"
    assert "rewrote three bullets" in (packet.path / "review.txt").read_text()
    assert any("adopted" in note for note in packet.notes)


def test_a_packet_with_both_pieces_is_complete(tmp_path):
    cfg = Cfg(tmp_path)
    packet = build_packet(cfg, FakeJob(), letter_text="letter\n")
    (packet.path / RESUME_FILE).write_bytes(b"docx")

    again = build_packet(cfg, FakeJob())

    assert again.complete
    assert again.missing() == []
