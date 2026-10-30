"""Each job's title nearest the role applied for, from the person's own list (ml/resume/titles.py)."""

from ml.resume import titles
from ml.resume.parser import Resume, Section

CLOUD = "CLOUD ENGINEER, CONTOSO LTD, USA  \tJan 2015 – Jun 2017"
LEAD = "LEAD DEVOPS ENGINEER, CONTOSO LTD, USA  \tJul 2017 – Dec 2021"
HEADLINE = "Sr. DevOps / Azure DevOps Engineer | Azure & AWS | Terraform"


def listed(tmp_path, entries):
    import yaml
    path = tmp_path / "titles.yaml"
    path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def resume():
    lines = ["HEADER", "Jordan Example", HEADLINE, "EXPERIENCE", LEAD, "- Ran CI/CD.", CLOUD, "- Built VNets."]
    return Resume(raw_text="\n".join(lines), sections=[
        Section("HEADER", ["Jordan Example", HEADLINE]),
        Section("EXPERIENCE", [LEAD, "- Ran CI/CD.", CLOUD, "- Built VNets."])])


def test_the_title_nearest_the_posting_and_the_own_one_on_a_tie():
    own, alt = "CLOUD ENGINEER", "CLOUD ARCHITECT / ENGINEER"
    assert titles.nearest([own, alt], "Generative AI Solutions Architect", "architect solutions", own) == alt
    assert titles.nearest([own, alt], "Site Reliability Engineer", "on call, incidents", own) == own
    # "architect" is in "architecture": the longer forms of a word count.
    assert titles.nearest([own, alt], "Platform Engineer", "design and architecture of systems", own) == alt


def test_only_the_title_changes_and_only_for_a_listed_job(tmp_path):
    path = listed(tmp_path, [{"role": CLOUD, "titles": ["CLOUD ENGINEER", "CLOUD ARCHITECT / ENGINEER"]}])
    r = resume()
    notes = titles.apply(r, "Solutions Architect", "Architect cloud solutions for customers.", path=path)
    assert notes == ["CLOUD ARCHITECT / ENGINEER, not CLOUD ENGINEER"]
    assert r.sections[1].lines[2] == "CLOUD ARCHITECT / ENGINEER, CONTOSO LTD, USA  \tJan 2015 – Jun 2017"
    assert r.sections[1].lines[0] == LEAD                                        # not listed: as written
    assert "CLOUD ARCHITECT / ENGINEER, CONTOSO LTD" in r.raw_text and "\nCLOUD ENGINEER," not in r.raw_text


def test_a_posting_nearer_the_written_title_leaves_the_resume_alone(tmp_path):
    path = listed(tmp_path, [{"role": CLOUD, "titles": ["CLOUD ENGINEER", "CLOUD ARCHITECT / ENGINEER"]}])
    r = resume()
    assert titles.apply(r, "Cloud Engineer", "Run Azure networking.", path=path) == []
    assert r.sections[1].lines[2] == CLOUD


def test_the_headline_is_chosen_whole_and_nothing_unlisted_is_ever_used(tmp_path):
    ai = "Sr. DevOps / Cloud Engineer | AWS | Kubernetes | AI Agents | Python"
    path = listed(tmp_path, [{"role": "HEADLINE", "titles": [HEADLINE, ai]},
                             {"role": LEAD, "titles": ["LEAD DEVOPS ENGINEER"]}])          # one title: no choice
    r = resume()
    titles.apply(r, "Forward Deployed Engineer", "Build AI agents on AWS with Kubernetes and Python.", path=path)
    assert r.sections[0].lines[1] == ai
    assert r.sections[1].lines[0] == LEAD


def test_no_list_changes_nothing(tmp_path):
    r = resume()
    assert titles.apply(r, "Solutions Architect", "architect", path=tmp_path / "none.yaml") == []
    assert r.sections[1].lines[2] == CLOUD
