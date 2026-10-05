"""Drafting again until the keyword match reaches the target, keeping the best draft."""

from ml.tailoring import rounds


class Draft:
    def __init__(self, n, accepted=True):
        self.n, self.accepted = n, accepted


def run(monkeypatch, scores, fixable=("kubernetes",)):
    made, asked = [], []
    monkeypatch.setattr(rounds, "ats_of", lambda draft, jd, company=None: {
        "before": 50, "after": scores[draft.n], "fixable": list(fixable), "concepts": []})

    def make(extra, focus=None):
        asked.append(extra)
        made.append(Draft(len(made)))
        return made[-1]
    return rounds.best_draft(make, "jd"), asked


def test_it_stops_as_soon_as_the_target_is_reached(monkeypatch):
    (draft, ats, used), _ = run(monkeypatch, [62, 84, 90])
    assert used == 2 and draft.n == 1 and ats["after"] == 84


def test_it_never_settles_below_the_original_resume(monkeypatch):
    monkeypatch.setattr(rounds, "ats_of", lambda draft, jd, company=None: {
        "before": 92, "after": [85, 93, 99][draft.n], "fixable": ["kubernetes"], "concepts": []})
    made = []
    def make(extra, focus=None):
        made.append((extra, focus)); d = Draft(len(made) - 1); return d
    draft, ats, used = rounds.best_draft(make, "jd")
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
