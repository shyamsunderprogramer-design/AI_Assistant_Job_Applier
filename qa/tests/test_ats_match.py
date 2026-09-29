"""The ATS check: the tailored resume against its posting, gaps split honestly."""

from ml.resume.ats import compare, requirements_only

JD = """Senior Platform Engineer. Build Terraform modules on AWS, run Kubernetes with
ArgoCD, and own Prometheus alerting. Terraform, Kubernetes and Kafka required.

Compensation: the range is $150,000 - $200,000. Paid time off, 401(k) match and
market-leading benefits.

We are an equal opportunity employer and do not discriminate."""

FULL = "Built Terraform modules on AWS. Ran Kubernetes with ArgoCD. Owned Prometheus alerting."
CUT = "Built Terraform modules on AWS."


def test_pay_and_legal_paragraphs_are_not_requirements():
    core = requirements_only(JD)
    assert "Terraform" in core
    assert "401(k)" not in core and "discriminate" not in core


def test_what_was_lost_is_fixable_and_what_was_never_there_is_a_gap():
    result = compare(FULL, CUT, JD)
    assert result["after"] < result["before"]
    assert "kubernetes" in result["fixable"] and "argocd" in result["fixable"]
    assert "kafka" in result["gaps"] and "kafka" not in result["fixable"]
    assert not {"paid", "market", "benefits"} & set(result["gaps"] + result["fixable"])


def test_no_tailored_resume_yet():
    result = compare(FULL, None, JD)
    assert result["after"] is None and result["fixable"] == []


def test_a_worse_rewrite_puts_the_better_one_back(tmp_path):
    import json
    from ml.resume.pipeline import _restore, _snapshot
    target = tmp_path / "acme_sre.docx"
    target.write_bytes(b"old docx")
    target.with_suffix(".pdf").write_bytes(b"old pdf")
    (tmp_path / "acme_sre_ats.json").write_text(json.dumps({"after": 77}))
    previous = _snapshot(target)
    assert previous["after"] == 77
    target.write_bytes(b"new docx")
    target.with_suffix(".pdf").write_bytes(b"new pdf")
    _restore(previous)
    assert target.read_bytes() == b"old docx" and target.with_suffix(".pdf").read_bytes() == b"old pdf"


def test_a_first_resume_has_nothing_to_compare_with(tmp_path):
    from ml.resume.pipeline import _snapshot
    assert _snapshot(tmp_path / "none.docx")["after"] is None


class _Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _loop(monkeypatch, scores, fixable_until=99):
    """Run tailor_to_target with a fake tailoring whose ATS scores are `scores`."""
    from ml.resume import ats as ats_mod, pipeline
    from ml.resume.pipeline import JobOutcome
    calls = []

    def fake_tailor(cfg, job_ids, align=False, **kw):
        calls.append(align)
        return [JobOutcome(1, "Acme", "SRE", 0.5, "tailored", "", None)]

    class Job:
        company, title = "Acme", "SRE"

    class Session:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a): return Job()
        def expunge(self, *a): pass

    def fake_check(cfg, job):
        n = len(calls)
        return {"before": 60, "after": scores[n - 1], "fixable": ["kafka"] if n < fixable_until else [],
                "concepts": [], "tools": ["protobuf"]}

    monkeypatch.setattr(pipeline, "tailor_jobs", fake_tailor)
    monkeypatch.setattr(pipeline, "get_session", lambda: Session())
    monkeypatch.setattr(ats_mod, "check", fake_check)
    _, result = pipeline.tailor_to_target(_Cfg({"resume.ats_target": 80, "resume.max_attempts": 3}), 1)
    return calls, result


def test_it_stops_as_soon_as_the_target_is_reached(monkeypatch):
    calls, result = _loop(monkeypatch, [70, 82, 90])
    assert calls == [False, True] and result["after"] == 82 and result["rounds"] == 2


def test_it_stops_when_nothing_honest_is_left_to_gain(monkeypatch):
    from ml.resume.pipeline import target_summary
    calls, result = _loop(monkeypatch, [70, 74, 76], fixable_until=2)
    assert len(calls) == 2
    line = target_summary(result)
    assert "best honest match" in line and "protobuf" in line


def test_it_gives_up_after_the_last_round(monkeypatch):
    calls, result = _loop(monkeypatch, [60, 65, 70])
    assert len(calls) == 3 and result["after"] == 70


def test_a_round_that_does_not_help_ends_it(monkeypatch):
    calls, _ = _loop(monkeypatch, [74, 74, 90])
    assert len(calls) == 2


def test_lone_words_are_never_offered_for_framing():
    from ml.resume.ats import split_gaps
    jd = "Hands-on cloud-based work for federal customers. Online data stores and on-premise fleets."
    concepts, _ = split_gaps(["hands", "based", "federal", "online data stores", "on-premise"], jd)
    assert concepts == ["online data stores", "on-premise"]
