"""A model reply cut off by its context limit, or broken by one bad comma, must
not lose the whole tailoring: the complete part is kept, and the fabrication
guard still checks whatever survives. Seen live on 26 Sep 2026 with gemma4:e4b
("Expecting ',' delimiter: line 88 column 6")."""

import pytest

from ml.resume.llm import extract_json, repair_json


def test_a_reply_cut_off_mid_bullet_keeps_the_complete_bullets():
    cut = '{"summary": "SRE", "bullets": [{"original": "a", "tailored": "b"}, {"original": "c", "tail'
    got = extract_json(cut)
    assert got["summary"] == "SRE"
    assert got["bullets"][0] == {"original": "a", "tailored": "b"}
    # A half-written last bullet may survive with no rewrite; the writer only
    # applies bullets that have both, so it changes nothing.
    assert all("tailored" not in b for b in got["bullets"][1:])


def test_a_missing_comma_keeps_everything_before_it():
    bad = '{"summary": "x", "gaps": ["k8s"], "bullets": [{"original": "a" "tailored": "b"}]}'
    assert extract_json(bad) == {"summary": "x", "gaps": ["k8s"]}


def test_quotes_and_brackets_inside_strings_are_not_structure():
    text = '{"a": "say \\"hi\\" [not a list] {nor this}", "b": ["x", "y'
    assert extract_json(text) == {"a": 'say "hi" [not a list] {nor this}', "b": ["x"]}


def test_valid_json_is_untouched():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}


def test_nothing_salvageable_still_fails_loudly():
    with pytest.raises(ValueError):
        extract_json("no json here")
    assert repair_json('{"a": ') is None
