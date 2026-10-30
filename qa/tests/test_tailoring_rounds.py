"""Drafting again until the keyword match reaches the target, keeping the best draft."""

from ml.tailoring import rounds


class Draft:
    def __init__(self, n, accepted=True):
        self.n, self.accepted = n, accepted


def run(monkeypatch, scores, fixable=("kubernetes",), **kw):
    made, asked = [], []
    monkeypatch.setattr(rounds, "ats_of", lambda draft, jd, company=None: {
        "before": 50, "after": scores[draft.n], "fixable": list(fixable), "concepts": []})

    def make(extra, focus=None):
        asked.append(extra)
        made.append(Draft(len(made)))
        return made[-1]
    return rounds.best_draft(make, "jd", **kw), asked


def test_it_stops_as_soon_as_the_target_is_reached(monkeypatch):
    (draft, ats, used), _ = run(monkeypatch, [62, 84, 90], target=80)
    assert used == 2 and draft.n == 1 and ats["after"] == 84


def test_it_aims_for_every_term_the_resume_can_show(monkeypatch):
    # 21 Oct 2026: the person asked for a 100% match; 84% with terms still to bring back is not done.
    (draft, ats, used), _ = run(monkeypatch, [62, 84, 90])
    assert rounds.TARGET == 100 and used == 3 and ats["after"] == 90


def test_it_never_settles_below_the_original_resume(monkeypatch):
    monkeypatch.setattr(rounds, "ats_of", lambda draft, jd, company=None: {
        "before": 92, "after": [85, 93, 99][draft.n], "fixable": ["kubernetes"], "concepts": []})
    made = []
    def make(extra, focus=None):
        made.append((extra, focus)); d = Draft(len(made) - 1); return d
    draft, ats, used = rounds.best_draft(make, "jd", target=80)
    assert used == 2 and ats["after"] == 93 and made[1][1] == ["kubernetes"]


def test_a_worse_round_never_replaces_a_better_one(monkeypatch):
    (draft, ats, used), asked = run(monkeypatch, [70, 61, 66])
    assert used == 3 and draft.n == 0 and ats["after"] == 70
    assert asked[0] == "" and "kubernetes" in asked[1] and "70%" in asked[1]


def test_nothing_left_to_bring_back_means_one_round(monkeypatch):
    (draft, ats, used), _ = run(monkeypatch, [55, 90], fixable=())
    assert used == 1 and ats["after"] == 55


def test_a_line_that_proves_a_requirement_is_never_dropped_for_space(tmp_path):
    from ml.resume.fit import fit_to_pages
    from ml.resume.parser import parse_resume
    from ml.resume.tailor import TailorResult
    path = tmp_path / "r.txt"
    path.write_text("Jordan Example\nEXPERIENCE\nEngineer, Acme     Jan 2020 – Present\n"
                    "- Ran Kubernetes clusters for 40 services.\n- Wrote Terraform modules.\n"
                    "- Organised the team offsite.\n- Kept the wiki tidy.\n")
    base = parse_resume(path)
    result = TailorResult(summary=None)
    result.omitted = [{"original": "- Ran Kubernetes clusters for 40 services.", "reason": "space"},
                      {"original": "- Organised the team offsite.", "reason": "space"}]
    fit_to_pages(base, result, "Kubernetes", max_pages=2, pages=lambda b, r: 1,
                 keep={"Ran Kubernetes clusters for 40 services."})
    left_out = " ".join(e["original"] for e in result.omitted)
    assert "Kubernetes" not in left_out and "offsite" in left_out


def test_the_company_name_and_its_city_are_not_keywords_to_match():
    from ml.resume.ats import compare
    jd = ("Platform Engineer at Catawiki in Lisbon. Catawikians improve the platform every day.\n"
          "You run Kubernetes and Terraform, and build CI/CD pipelines on AWS.")
    resume = ("Platform engineer: build and run Kubernetes clusters on AWS every day; "
              "Terraform modules; CI/CD pipelines.")
    result = compare(resume, None, jd, company="Catawiki")
    assert result["before"] == 100, result          # catawikians, lisbon, improve do not count


# -- the resume is the only evidence (30 Oct 2026, audit RT-01) ----------------------------------
# From 22 Oct round two was told to "Add a skill if not already present, tool or claim the resume
# does not contain". Made-up resume, posting and draft; the keyword match is the real one.

MASTER = """JORDAN EXAMPLE
jordan@example.com

EXPERIENCE
PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present
- Built Terraform modules for 200+ AWS workloads.
- Ran Kubernetes clusters on EKS.
SYSTEMS ENGINEER, GLOBEX, USA     Jan 2016 – Dec 2021
- Maintained VMware clusters.
"""
ROLE = "PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present"
JD = "You will define Protobuf schemas, run Kafka and Kubernetes, and build Terraform modules on AWS."
DRAFT = "PLATFORM ENGINEER, ACME, USA Jan 2022 – Present. Ran Kubernetes clusters on EKS for AWS workloads."


def round_two(monkeypatch, resume_text):
    """The keyword match of the draft against this resume, and what round two is told."""
    from ml.resume.ats import compare
    measured = compare(resume_text, DRAFT, JD)
    monkeypatch.setattr(rounds, "ats_of", lambda draft, jd, company=None: measured)
    asked = []

    def make(extra, focus=None):
        asked.append(extra)
        return Draft(len(asked) - 1)
    rounds.best_draft(make, JD, rounds=2)
    return measured, asked[1]


def test_a_skill_only_the_posting_names_is_never_asked_for_and_stays_a_gap(monkeypatch):
    from ml.resume.parser import parse_text
    measured, told = round_two(monkeypatch, parse_text(MASTER).text())
    assert {"kafka", "protobuf"} <= set(measured["tools"]) and not {"kafka", "protobuf"} & set(measured["fixable"])
    assert "kafka" not in told.lower() and "protobuf" not in told.lower()
    assert "terraform" in told.split("truthfully fits:")[1].splitlines()[0]   # the resume's own, lost by the draft
    assert "Never add a" in told and "is not evidence the person has it" in told
    assert "Never present related or similar work as direct experience" in told
    assert "stays a gap" in told and "never add words to reach it" in told
    assert "Add a skill if not already present" not in rounds.ALIGN


def test_a_skill_the_person_added_and_confirmed_may_come_back_and_the_posting_alone_still_adds_nothing(
        monkeypatch, tmp_path):
    """Not in the uploaded resume, but added from a job page with "I've used this" and ticked true
    (ml/resume/additions.py): that is evidence. Kafka, still only in the posting, is not."""
    from ml.resume import additions
    from ml.resume.parser import parse_text
    store = tmp_path / "additions.yaml"
    additions.add(parse_text(MASTER), ROLE, "Defined Protobuf schemas for service contracts", "Protobuf", True,
                  path=store)
    measured, told = round_two(monkeypatch, additions.apply(parse_text(MASTER), path=store).text())
    assert "protobuf" in measured["fixable"] and "kafka" in measured["tools"]
    assert "protobuf" in told.split("truthfully fits:")[1].splitlines()[0]
    assert "kafka" not in told.lower()
