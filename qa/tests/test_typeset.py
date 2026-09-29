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
