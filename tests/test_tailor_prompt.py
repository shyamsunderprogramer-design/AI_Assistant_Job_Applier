"""What the tailoring prompt must keep telling the model.

These are not style preferences. Each one is a defect gemma4:e4b actually
produced on a real Buyer posting, measured 2026-09-18:

    bullets longer than the original : 6 of 6
    clauses arguing the application  : 3
    tense changed on a current role  : 3

The tense one is the worst: the resume says "Analyze" because it is the job
he still holds, and "Analyzed" tells a recruiter he has left.
"""

from resume.tailor import SYSTEM_PROMPT


def test_the_prompt_forbids_changing_tense():
    assert "KEEP THE TENSE" in SYSTEM_PROMPT
    assert "Never change the tense" in SYSTEM_PROMPT


def test_the_prompt_caps_the_length():
    assert "SAME LENGTH OR SHORTER" in SYSTEM_PROMPT


def test_the_prompt_forbids_arguing_the_case_inside_a_bullet():
    assert "NEVER EXPLAIN THE RELEVANCE INSIDE THE BULLET" in SYSTEM_PROMPT
    # The three phrases it actually produced, named so it recognises them.
    for phrase in ("directly supporting", "mirroring", "demonstrating ability"):
        assert phrase in SYSTEM_PROMPT


def test_the_prompt_permits_leaving_a_bullet_alone():
    """Without this the model pads rather than return the bullet unchanged."""
    assert "unchanged bullet is a perfectly good answer" in SYSTEM_PROMPT


def test_the_prompt_shows_a_worked_example():
    """A smaller model follows a demonstration better than a rule."""
    assert "BAD" in SYSTEM_PROMPT and "GOOD" in SYSTEM_PROMPT


def test_the_fabrication_rules_are_still_there():
    """The rewrite guidance must never displace the honesty rules."""
    assert "you may not invent anything" in SYSTEM_PROMPT
    assert "A\ntruthful weaker match is the correct output" in SYSTEM_PROMPT
    assert '"gaps"' in SYSTEM_PROMPT
