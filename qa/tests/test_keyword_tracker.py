"""The keyword tracker: shown and where, lost in tailoring, not on the resume; edits checked before saving."""

import json
from types import SimpleNamespace as NS

import pytest

from ml.tailoring import keywords, pipeline


@pytest.fixture
def posting(monkeypatch):
    monkeypatch.setattr(keywords, "weights", lambda jd, company=None: {
        "terraform": 3.0, "kafka": 2.0, "kubernetes": 2.0, "dynatrace": 1.0})


MASTER = "Built Terraform modules.\nRan Kafka streams.\nManaged Kubernetes clusters.\n"
TAILORED = "Built Terraform modules.\nManaged Kubernetes clusters.\nTerraform again for staging.\n"


def test_each_keyword_is_shown_lost_or_missing(posting):
    out = keywords.track(MASTER, TAILORED, "jd", None)
    by = {k["term"]: k for k in out["keywords"]}
    assert by["terraform"]["status"] == "shown"
    assert by["terraform"]["where"] == ["Built Terraform modules.", "Terraform again for staging."]
    assert by["kafka"]["status"] == "lost" and by["kafka"]["master_lines"] == ["Ran Kafka streams."]
    assert by["dynatrace"]["status"] == "missing"
    assert out["counts"] == {"shown": 2, "lost": 1, "missing": 1}
    assert out["coverage"] == round(100 * 5 / 8)
    assert out["keywords"][0]["term"] == "kafka"            # lost first: the ones to act on


def test_an_edit_loses_a_keyword_only_when_no_other_line_has_it(posting):
    one = keywords.check_edits(TAILORED, {"Built Terraform modules.": ""}, "jd")
    assert one["lost"] == []                                # still in "Terraform again"
    both = keywords.check_edits(TAILORED, {"Managed Kubernetes clusters.": "Managed clusters."}, "jd")
    assert both["lost"] == ["kubernetes"]
    assert both["by_line"] == {"Managed Kubernetes clusters.": ["kubernetes"]}
    assert both["coverage_after"] < both["coverage_before"]


def test_an_edit_that_adds_a_keyword_says_so(posting):
    out = keywords.check_edits(TAILORED, {"Managed Kubernetes clusters.": "Managed Kubernetes and Kafka."}, "jd")
    assert out["gained"] == ["kafka"] and out["lost"] == []


def test_a_cut_line_is_put_back_and_the_score_redone(tmp_path, monkeypatch):
    run = tmp_path / "run"; (run / "inputs").mkdir(parents=True)
    (run / "result.json").write_text(json.dumps({"before_after": {"ats": [90, 80]}}))
    result = NS(omitted=[{"original": "Ran Kafka streams."}, {"original": "Other line."}],
                bullets=[{"original": "Built Terraform modules.", "tailored": "Terraform modules built."}])
    draft = NS(result=result)
    monkeypatch.setattr(pipeline, "load_draft", lambda f: (draft, json.loads((f / "result.json").read_text())))
    monkeypatch.setattr(pipeline, "_draft_json", lambda d: {})
    monkeypatch.setattr(pipeline, "_rescore", lambda f, data: data["before_after"].update(ats=[90, 92]))
    monkeypatch.setattr(pipeline, "_place_if_job", lambda f, data: None)
    from ml.tailoring import documents
    monkeypatch.setattr(documents, "produce", lambda *a, **k: NS(
        docx=run / "t.docx", pdf=run / "t.pdf", pages={"pdf": ["p1.png"]}, checks=[], ok=True))

    data = pipeline.restore_line(run, "• Ran Kafka streams.")
    assert result.omitted == [{"original": "Other line."}]
    assert data["before_after"]["ats"] == [90, 92] and data["restored"] == ["• Ran Kafka streams."]

    pipeline.restore_line(run, "Built Terraform modules.")      # a rewrite undone: the original back
    assert result.bullets == []

    with pytest.raises(ValueError):
        pipeline.restore_line(run, "A line the tailoring never touched.")
