"""The posting's tools and skills in bold where the resume names them."""

from ml.resume import emphasis


def test_only_named_tools_and_phrases_are_kept(monkeypatch):
    from ml.resume import ats
    monkeypatch.setattr(ats, "compare", lambda *a, **k: {"matched": [
        "kubernetes", "terraform", "management", "best", "infrastructure as code", "ci", "cd"]})
    text = ("Ran Kubernetes clusters with Terraform modules. Management of best practices "
            "and infrastructure as code in CI/CD pipelines.")
    kept = emphasis.terms("jd", text)
    assert set(kept) == {"kubernetes", "terraform", "infrastructure as code", "ci/cd"}


def test_a_line_bolds_each_keyword_once_and_at_most_three():
    keys = ["infrastructure as code", "kubernetes", "terraform", "azure", "ci/cd"]
    text = "Built Terraform and Azure CI/CD for Kubernetes and Terraform again."
    pieces = emphasis.spans(text, keys)
    bold = [p for p, b in pieces if b]
    assert bold == ["Terraform", "Azure", "CI/CD"]
    assert "".join(p for p, _ in pieces) == text            # nothing added or lost


def test_a_longer_phrase_wins_over_a_word_inside_it():
    pieces = emphasis.spans("Wrote infrastructure as code daily.", ["infrastructure as code", "code"])
    assert [p for p, b in pieces if b] == ["infrastructure as code"]


def test_a_word_capitalised_only_in_a_group_title_is_not_a_name():
    text = "Application Development & Delivery\nBuilt services with Kafka and React.\n"
    assert not emphasis._named("application", text)
    assert not emphasis._named("development", text)
    assert emphasis._named("kafka", text) and emphasis._named("react", text)


def test_a_product_name_is_bolded_whole():
    pieces = emphasis.spans("Used Azure Key Vault and Spring Boot daily.", ["azure", "vault", "spring"])
    assert [p for p, b in pieces if b] == ["Azure Key Vault", "Spring Boot"]


def test_two_keywords_in_one_name_make_one_phrase():
    pieces = emphasis.spans("Pipelines in Azure DevOps for teams.", ["devops", "azure"])
    assert [p for p, b in pieces if b] == ["Azure DevOps"]


def test_a_groups_label_and_a_word_of_a_longer_name_are_not_names():
    text = ("Version Control & Artifact Management: GitHub, GitLab\nDatabases: MySQL, MS SQL Server\n"
            "Enforced access control. Used vCenter Server and Jenkins daily.")
    assert not emphasis._named("control", text)           # "access **control**" was bold
    assert not emphasis._named("server", text)            # "vCenter **Server**" was bold
    assert emphasis._named("jenkins", text) and emphasis._named("github", text)
    assert emphasis._named("kubernetes", "Ran Kubernetes clusters daily.")   # a sentence's first word is no name's


def test_a_name_is_bold_whole_and_once_per_job():
    used: set[str] = set()
    first = emphasis.spans("Built CI/CD in Jenkins 2.x and Azure DevOps.", ["ci/cd", "jenkins", "devops"], used=used)
    assert [p for p, b in first if b] == ["CI/CD", "Jenkins", "Azure DevOps"]   # not "Azure **DevOps**"
    again = emphasis.spans("Added Snyk to Jenkins and Azure DevOps.", ["ci/cd", "jenkins", "devops"], used=used)
    assert not any(b for _, b in again)                                    # bold once per job, not every bullet


def test_skill_items_are_not_bold():
    from docx import Document
    from ml.resume.writer import _add_skill
    doc = Document()
    _add_skill(doc, "CI/CD & GitOps: Jenkins, Azure DevOps, ArgoCD", ["jenkins", "devops"])
    runs = doc.paragraphs[-1].runs
    assert runs[0].bold and runs[0].text == "CI/CD & GitOps: "
    assert not any(r.bold for r in runs[1:])
