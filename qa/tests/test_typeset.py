"""A resume and letter typed like a person typed them."""

import pytest

from ml.resume.typeset import tells_in, typeset


@pytest.mark.parametrize("raw,clean", [
    ("45 % faster", "45% faster"),
    ("11 + years of", "11+ years of"),
    ("~ 30 % lower spend", "~30% lower spend"),
    ("Monitoring — built dashboards", "Monitoring - built dashboards"),
    ("Jan 2023 – Dec 2024", "Jan 2023 – Dec 2024"),        # date ranges keep the en dash
    ("GitHub / Azure  DevOps", "GitHub / Azure DevOps"),
    ("C++ and C# and 200+ workloads", "C++ and C# and 200+ workloads"),
])
def test_spacing_and_dashes(raw, clean):
    assert typeset(raw) == clean


def test_machine_words_are_named():
    assert tells_in("Spearheaded a robust, seamless migration") == ["spearhead", "seamless", "robust"]
    assert tells_in("Built and ran the release pipeline") == []


def test_role_lines_keep_the_space_that_separates_their_dates():
    from ml.resume.parser import parse_text
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import plan_document
    base = parse_text("JANE DOE\njane@example.com\n\nEXPERIENCE\n"
                      "SENIOR ENGINEER, ACME, USA     Jan 2022 – Present\n- Cut costs by 30 %.\n")
    blocks = dict((k, v) for k, v in plan_document(base, TailorResult(summary=None)))
    assert "     Jan 2022" in blocks["role"] and blocks["bullet"] == "Cut costs by 30%."


def test_plain_words_swap_verbs_and_keep_real_terms():
    from ml.resume.typeset import plain_words
    assert plain_words("Spearheaded a robust rollout, utilizing Helm") == "Led a reliable rollout, using Helm"
    assert plain_words("Dynamic Senior DevOps Engineer") == "Senior DevOps Engineer"
    assert plain_words("Cut CPU utilization by 30%") == "Cut CPU utilization by 30%"


def test_a_state_code_is_capitalised():
    from ml.resume.typeset import state_codes
    assert state_codes("Kakinada, Ap, India") == "Kakinada, AP, India"
    assert state_codes("Austin, Tx, USA") == "Austin, TX, USA"


ARRANGE = """JANE DOE
jane@example.com

SUMMARY
- Platform engineer with 10 years across cloud.
- Runs Kubernetes and Terraform in production.

EDUCATION
BS Computer Science, State University     2012 – 2016

HIGHLIGHTS OF IMPACT
- Cut deploy time by 40%.

TECHNICAL SKILLS
Cloud & Platform Engineering
- Hands-on experience across AWS (EKS, S3, Lambda) and Azure (AKS, Functions) for production workloads.
Databases & Data Services
- NoSQL and streaming with DynamoDB, Cassandra, Redis and Kafka across several teams.
IDEs / Servers / Operating Systems
- Operating Systems: Windows, Linux, macOS and UNIX across build and production estates.

EXPERIENCE
PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present
- Built Terraform modules for 200+ workloads.
"""


def test_sections_are_ordered_and_tidied():
    from ml.resume.parser import parse_text
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import plan_document
    blocks = plan_document(parse_text(ARRANGE), TailorResult(summary=None))
    headings = [t for k, t in blocks if k == "heading"]
    assert blocks[0] == ("name", "JANE DOE")                            # a capitalised name is the header
    assert headings == ["SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "EDUCATION"]
    assert ("text", "Platform engineer with 10 years across cloud.") in blocks   # summary as prose
    assert ("text", "Runs Kubernetes and Terraform in production.") in blocks
    assert ("skill", "Cloud & Platform Engineering: Hands-on experience across AWS (EKS, S3, Lambda) "
                     "and Azure (AKS, Functions) for production workloads.") in blocks   # one line
    assert not any("Cut deploy time" in t for _, t in blocks)          # one-bullet side section gone
    assert ("skill", "IDEs / Servers / Operating Systems: Windows, Linux, macOS and UNIX across "
                     "build and production estates.") in blocks                  # label not repeated



def test_utilization_is_a_real_word():
    from ml.resume.typeset import tells_in
    assert tells_in("Improved resource utilization by 25%") == []
    assert tells_in("Utilized Helm charts") == ["utilize"]


def test_a_kept_resume_is_redrawn_from_its_saved_content(tmp_path):
    from ml.resume.parser import parse_text
    from ml.resume.pipeline import _redraw, _save_result
    from ml.resume.tailor import TailorResult
    base = parse_text(ARRANGE)
    target = tmp_path / "acme.docx"
    _save_result(TailorResult(summary="Platform engineer."), target)
    assert _redraw(base, target) and target.exists()
    assert not _redraw(base, tmp_path / "never.docx")



def test_a_skills_section_named_summary_is_still_skills():
    from ml.resume.writer import _rank
    assert _rank("Core Competencies / Technical Skills Summary") == _rank("Technical Skills")
    assert _rank("Professional Summary") == 0
