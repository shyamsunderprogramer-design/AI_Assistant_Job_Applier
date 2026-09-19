"""Cover-letter tests — offline, no API.

The guard is the whole point of this module. A letter goes out under the
candidate's name, so the tests that matter are the ones proving it cannot claim
something the resume does not support — including when the posting asks for it.
"""

import pytest

from resume.letter import (
    LetterResult,
    build_prompt,
    check_letter,
    parse_reply,
    result_from_reply,
)
from resume.parser import Resume

RESUME_TEXT = """SHYAM SUNDER
Cloud Engineer

EXPERIENCE
Acme Corp — Senior Cloud Engineer, 2019 to 2024
• Ran Kubernetes clusters serving 40 million requests a day
• Migrated 30 services to Terraform
• Cut deployment time by 60%

SKILLS
Kubernetes, Terraform, AWS, Python, Jenkins
"""

JD_TEXT = """Senior Platform Engineer at ClickUp

ClickUp is building the everything app. You will work on our Hyperspace
platform alongside the Foundry team.

Requirements:
- 5+ years with Kubernetes
- Experience with Datadog and Snowflake
- Rust proficiency preferred
"""


def resume() -> Resume:
    return Resume(raw_text=RESUME_TEXT, sections=[])


def letter(*body, why="") -> str:
    return "\n".join(body + (why,))


# -- the asymmetry: names are quoted, claims are checked --------------------

def test_naming_the_hiring_company_is_not_fabrication():
    """Every letter names the employer, and no resume contains it."""
    body = "I would like to join ClickUp because the platform work fits my background."

    assert check_letter(RESUME_TEXT, JD_TEXT, body).ok


def test_naming_the_companys_products_is_not_fabrication():
    body = "The Hyperspace platform and the Foundry team are doing what I have done before."

    assert check_letter(RESUME_TEXT, JD_TEXT, body).ok


def test_a_company_not_in_the_posting_is_still_flagged():
    # Nothing excuses an employer that appears in neither document.
    body = "At Netflix I ran the same kind of platform."
    verdict = check_letter(RESUME_TEXT, JD_TEXT, body)

    assert not verdict.ok
    assert any(v.value == "Netflix" for v in verdict.violations)


def test_a_skill_the_posting_asks_for_is_still_fabrication():
    """The heart of it: what an employer wants is not evidence you have it.

    Rust is all over the job description. That must not let the letter claim it.
    """
    body = "My Rust experience maps directly onto this role."
    verdict = check_letter(RESUME_TEXT, JD_TEXT, body)

    assert not verdict.ok
    assert any(v.kind == "skill" and v.value == "rust" for v in verdict.violations)


def test_another_posting_skill_is_also_still_fabrication():
    body = "I have run Datadog and Snowflake in production for years."
    verdict = check_letter(RESUME_TEXT, JD_TEXT, body)

    assert not verdict.ok
    assert {v.value for v in verdict.violations} & {"datadog", "snowflake"}


def test_a_skill_the_resume_has_passes():
    body = "I have run Kubernetes and Terraform at scale."

    assert check_letter(RESUME_TEXT, JD_TEXT, body).ok


def test_an_invented_metric_is_flagged():
    body = "I cut costs by 85% in my last role."
    verdict = check_letter(RESUME_TEXT, JD_TEXT, body)

    assert not verdict.ok
    assert any(v.kind == "metric" for v in verdict.violations)


def test_a_metric_from_the_resume_passes():
    body = "I cut deployment time by 60% at Acme."

    assert check_letter(RESUME_TEXT, JD_TEXT, body).ok


def test_an_invented_year_is_flagged():
    body = "Since 2011 I have worked on platform teams."
    verdict = check_letter(RESUME_TEXT, JD_TEXT, body)

    assert not verdict.ok
    assert any(v.kind == "year" and v.value == "2011" for v in verdict.violations)


# -- reading a model's reply ------------------------------------------------

def test_a_clean_reply_is_accepted():
    reply = """{
      "greeting": "Dear Hiring Manager,",
      "body": ["I have run Kubernetes and Terraform at Acme Corp."],
      "closing": "Sincerely,\\nShyam Sunder",
      "why_this_company": "The platform work at ClickUp matches what I do.",
      "left_out": ["Rust — not in my background"]
    }"""
    result = result_from_reply(resume(), JD_TEXT, reply)

    assert result.accepted
    assert "Kubernetes" in result.text()
    assert result.left_out == ["Rust — not in my background"]


def test_a_fabricating_reply_is_rejected():
    reply = """{
      "body": ["I bring 9 years of Rust and Datadog experience."],
      "closing": "Sincerely"
    }"""
    result = result_from_reply(resume(), JD_TEXT, reply)

    assert not result.accepted
    assert result.guard.violations


def test_what_was_left_out_is_not_itself_guarded():
    """left_out names the skills the letter correctly refused to claim.

    Guarding it would reject a letter for honestly saying "I do not have Rust".
    """
    reply = """{
      "body": ["I have run Kubernetes at Acme Corp."],
      "left_out": ["Rust", "Datadog", "Snowflake"]
    }"""
    result = result_from_reply(resume(), JD_TEXT, reply)

    assert result.accepted


def test_a_reply_wrapped_in_a_code_fence_is_read():
    reply = '```json\n{"body": ["I have run Kubernetes."]}\n```'

    assert parse_reply(reply)["body"] == ["I have run Kubernetes."]


def test_an_empty_reply_is_a_clear_error():
    with pytest.raises(ValueError, match="empty"):
        parse_reply("   ")


def test_a_reply_without_a_body_is_a_clear_error():
    with pytest.raises(ValueError, match="body"):
        parse_reply('{"greeting": "Dear Hiring Manager,"}')


# -- an unguarded letter must never count as accepted -----------------------

def test_a_result_with_no_guard_is_not_accepted():
    # Fails CLOSED, unlike the resume path: prose in someone's name does not
    # get the benefit of the doubt.
    assert not LetterResult(body=["anything at all"]).accepted


# -- the prompt -------------------------------------------------------------

def test_the_prompt_carries_the_resume_and_the_posting():
    prompt = build_prompt(resume(), "Senior Platform Engineer", "ClickUp", JD_TEXT)

    assert "ClickUp" in prompt
    assert "Kubernetes" in prompt
    assert "ONLY source of truth" in prompt


# -- the posting's own words are not claims about the candidate -------------

def test_a_word_the_posting_uses_with_punctuation_is_still_its_word():
    """A real posting writes "FedRAMP®" and "FedRAMP," and never the bare
    word. Matching raw tokens meant a letter saying "FedRAMP" matched nothing
    and was rejected for quoting the posting it was answering."""
    from resume.letter import _from_posting, _vocabulary

    vocabulary = _vocabulary("Exiger is FedRAMP® authorized, serving government "
                             "and defense customers.")

    assert _from_posting("FedRAMP", vocabulary)
    assert _from_posting("Exiger", vocabulary)
    assert _from_posting("government", vocabulary)


def test_a_compound_built_from_the_postings_words_is_allowed():
    """Writing about an employer produces phrases the posting implies but
    never spells: "FedRAMP-authorized" from a posting that says "FedRAMP®"
    and "authorized". This rejected a correct letter."""
    from resume.letter import _from_posting, _vocabulary

    vocabulary = _vocabulary("Exiger is FedRAMP® authorized, serving government.")

    assert _from_posting("FedRAMP-authorized", vocabulary)


def test_a_compound_with_an_invented_half_is_still_caught():
    """The loosening must not let an invented word ride in attached to a real
    one. Every part has to come from the posting."""
    from resume.letter import _from_posting, _vocabulary

    vocabulary = _vocabulary("Exiger is FedRAMP® authorized.")

    assert not _from_posting("FedRAMP-certified", vocabulary)
    assert not _from_posting("SOC2-audited", vocabulary)
    assert not _from_posting("Palantir", vocabulary)


# --- the guard's own blind spot -------------------------------------------

def test_a_word_ending_a_sentence_is_not_treated_as_invented():
    """TOKEN_RE keeps "." inside tokens so node.js survives. The cost was that
    "operational KPIs." produced "kpis.", which never matched "KPIs" written
    mid-sentence -- so the guard rejected a real local-model tailoring for
    using a word printed twice in the resume."""
    from resume.guard import check_no_fabrication

    base = "Reported on operational KPIs. Worked with stakeholders."
    tailored = "Tracked KPIs and briefed stakeholders on performance."
    result = check_no_fabrication(base, tailored)
    assert result.ok, result.report()


def test_hyphenated_and_dotted_terms_still_match_themselves():
    from resume.guard import check_no_fabrication

    base = "Built services on Node.js and ASP.NET with cost-to-serve models."
    tailored = "Built Node.js services and cost-to-serve analysis on ASP.NET."
    assert check_no_fabrication(base, tailored).ok


def test_a_genuine_invention_is_still_caught():
    """Forgiving punctuation must not become forgiving everything."""
    from resume.guard import check_no_fabrication

    base = "Supply chain analyst. Reported on operational KPIs."
    tailored = "Supply chain analyst certified in Kubernetes and FedRAMP."
    result = check_no_fabrication(base, tailored)
    assert not result.ok
    flagged = {v.value.lower() for v in result.violations}
    assert "kubernetes" in flagged or "fedramp" in flagged


def test_an_ampersand_term_printed_in_the_resume_is_not_invented():
    """TOKEN_RE has no "&", so "S&OP" tokenises to "s" and "op" and the whole
    term is never in the vocabulary. gemma4:e4b was rejected for writing S&OP,
    which appears twice in the resume it was tailoring."""
    from resume.guard import check_no_fabrication

    base = "Supported S&OP and demand planning. Skills: MRP, MPS, S&OP, IBP."
    tailored = "Ran S&OP cycles and MRP planning."
    assert check_no_fabrication(base, tailored).ok


def test_a_multi_word_name_needs_every_word_evidenced():
    from resume.guard import check_no_fabrication

    base = "Built Power BI dashboards for demand planning."
    assert check_no_fabrication(base, "Built Power BI dashboards.").ok
    # "Tableau" appears nowhere, so the pair cannot borrow support from "BI".
    assert not check_no_fabrication(base, "Built Tableau BI dashboards.").ok


def test_decomposition_does_not_excuse_a_single_invented_word():
    """A one-word name has no parts to hide behind."""
    from resume.guard import check_no_fabrication

    base = "Supply chain analyst. Reported on operational KPIs."
    result = check_no_fabrication(base, "Certified in Kubernetes.")
    assert not result.ok


def test_half_of_a_hyphenated_phrase_is_not_invented():
    """The resume prints "Cross-Functional Collaboration". The entity check
    reads "Functional" as a name of its own, and the vocabulary held only the
    joined token -- so gemma4:e4b was rejected for a phrase copied verbatim."""
    from resume.guard import check_no_fabrication

    base = "Business Skills: Cross-Functional Collaboration, Stakeholder Management."
    tailored = "Led Cross-Functional teams. Functional ownership of planning."
    assert check_no_fabrication(base, tailored).ok


def test_compound_parts_do_not_excuse_an_unrelated_word():
    from resume.guard import check_no_fabrication

    base = "Cross-Functional Collaboration and demand planning."
    result = check_no_fabrication(base, "Cross-Functional work on Kubernetes.")
    assert not result.ok
    assert "kubernetes" in {v.value.lower() for v in result.violations}


def test_a_word_the_pdf_split_is_not_treated_as_invented():
    """PDF extraction letter-spaces words: this resume holds "CUMMINS INDIA
    L TD" and "requiremen ts". A model that spells the employer correctly then
    looks like it invented the name -- gemma4:e4b was rejected for 'Ltd'."""
    from resume.guard import check_no_fabrication

    base = "Worked at CUMMINS INDIA L TD on material requiremen ts planning."
    tailored = "At Cummins India Ltd, evaluated material requirements."
    assert check_no_fabrication(base, tailored).ok


def test_rejoining_does_not_invent_a_whole_employer():
    """Only adjacent pairs rejoin, and only where the second piece is too
    short to be a word. An unrelated company still has nothing to stand on."""
    from resume.guard import check_no_fabrication

    base = "Worked at CUMMINS INDIA L TD on procurement."
    result = check_no_fabrication(base, "Worked at Siemens on procurement.")
    assert not result.ok
    assert "siemens" in {v.value.lower() for v in result.violations}
