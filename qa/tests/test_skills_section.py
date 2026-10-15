"""One Skills section, one line per group, the tools and nothing else -- whatever shape the master had."""

from ml.resume import skills

# Laid out as a real master was: a group's label over its own bullets, two groups run together on one
# line, a description after every row's tools, and Helm, Python and Azure DevOps in several groups.
MASTER = [
    "Cloud Platforms: AWS (EC2, RDS, Lambda), Azure (AKS, Key Vault), GCP (GKE, Cloud Run) "
    "Containerization & Orchestration: Docker (multi-stage builds), Kubernetes (AKS, EKS), Helm, RBAC",
    "Infrastructure as Code & Automation: Terraform (v1.x), Pulumi, Helm (chart packaging), Python (3.x), Bash",
    "CI/CD & GitOps: GitLab CI, Azure DevOps, Flux (2.x), Helm (v3) – Implemented canary and rollback stages",
    "Security, DevSecOps & Compliance:",
    "Snyk – added Snyk scans to every pipeline for dependency checks.",
    "SonarQube Cloud – set quality gates for static analysis.",
    "Wiz familiarity – some exposure to cloud posture tools.",
    "HashiCorp Vault, Azure Key Vault, AWS KMS – stored secrets and keys.",
    "Ensured HIPAA/FedRAMP readiness with security gates in every pipeline.",
    "Monitoring & Observability: Prometheus, Grafana, New Relic – Used for alerts and dashboards",
    "Programming & Scripting Languages: Python (scripts for the Jira and GitHub APIs), Bash, "
    "Groovy (Jenkins DSL), Go – wrote internal tooling.",
    "Issue Tracking & ITSM Tools: Jira, ServiceNow, Azure DevOps – Tracked tickets and changes",
]


def test_each_group_is_one_line_of_tools():
    lines = skills.compact(MASTER)
    labels = [l.split(":")[0] for l in lines]
    assert labels == ["Cloud Platforms", "Containerization & Orchestration", "Infrastructure as Code & Automation",
                      "CI/CD & GitOps", "Security, DevSecOps & Compliance", "Monitoring & Observability",
                      "Programming & Scripting Languages", "Issue Tracking & ITSM Tools"]
    assert lines[0] == "Cloud Platforms: AWS (EC2, RDS, Lambda), Azure (AKS, Key Vault), GCP (GKE, Cloud Run)"
    assert lines[1].startswith("Containerization & Orchestration: Docker (multi-stage builds), Kubernetes")
    assert not any(" – " in l or "Implemented" in l or "Used for" in l for l in lines)   # no descriptions


def test_a_label_over_its_bullets_is_one_group():
    security = next(l for l in skills.compact(MASTER) if l.startswith("Security"))
    assert security == ("Security, DevSecOps & Compliance: Snyk, SonarQube Cloud, Wiz (familiar), "
                        "HashiCorp Vault, Azure Key Vault, AWS KMS, HIPAA, FedRAMP")


def test_each_tool_is_shown_once_and_languages_with_the_languages():
    lines = skills.compact(MASTER)
    text = " ".join(lines)
    assert text.count("Helm") == 1 and text.count("Azure DevOps") == 1
    iac = next(l for l in lines if l.startswith("Infrastructure"))
    assert "Python" not in iac and "Bash" not in iac
    assert next(l for l in lines if l.startswith("Programming")) == \
        "Programming & Scripting Languages: Python, Bash, Groovy (Jenkins DSL), Go"


def test_a_group_written_as_sentences_is_kept_as_written():
    row = "Cloud & Platform Engineering: Hands-on experience across AWS (EKS, S3) and Azure for production workloads."
    assert skills.compact([row]) == [row]
    phrase = "Databases: NoSQL and streaming with DynamoDB, Cassandra, Redis and Kafka across several teams."
    assert skills.compact([phrase]) == [phrase]


def test_the_import_keeps_one_skills_section(tmp_path):
    from docx import Document
    from ml.resume import builder
    doc = Document()
    doc.add_paragraph("Jordan Example")
    doc.add_paragraph("jordan@example.com · +1 555 010 0199")
    doc.add_paragraph("SKILLS")
    for line in MASTER:
        doc.add_paragraph(line)
    doc.add_paragraph("EXPERIENCE")
    doc.add_paragraph("PLATFORM ENGINEER, ACME, USA     Jan 2022 – Present")
    doc.add_paragraph("Built Terraform modules for 200+ workloads.")
    path = tmp_path / "master.docx"
    doc.save(path)
    data = builder.from_file(path)["data"]
    assert not any(e["heading"].lower() == "skills" for e in data["extra"])        # no second Skills section
    labels = [g["label"] for g in data["skills"]]
    assert "Security, DevSecOps & Compliance" in labels and "Containerization & Orchestration" in labels
    assert all(" – " not in g["items"] for g in data["skills"])


def test_two_skills_sections_print_as_one():
    from ml.resume.writer import _arrange
    out = _arrange([("name", "JORDAN EXAMPLE"), ("heading", "SKILLS"), ("text", "Cloud: AWS, Azure, GCP"),
                    ("heading", "EXPERIENCE"), ("role", "DEV, ACME   2020"), ("bullet", "Built things."),
                    ("heading", "SKILLS"), ("subheading", "Security & Compliance"),
                    ("bullet", "Snyk – integrated the CLI into pipelines."), ("bullet", "Trivy, tfsec – IaC scanning.")])
    assert [t for k, t in out if k == "heading"] == ["SKILLS", "EXPERIENCE"]
    assert [t for k, t in out if k == "skill"] == ["Cloud: AWS, Azure, GCP", "Security & Compliance: Snyk, Trivy, tfsec"]


def test_a_mixed_case_title_follows_the_others_into_capitals():
    from ml.resume.writer import _arrange
    out = _arrange([("heading", "EXPERIENCE"),
                    ("role", "Systems Analyst / SENIOR PLATFORM ENGINEER, Globex, USA     Mar 2024 – Present"),
                    ("bullet", "Built things."), ("role", "LEAD DEVOPS ENGINEER, ACME, IND     Apr 2018 – Dec 2022"),
                    ("bullet", "Built more."), ("role", "CLOUD ENGINEER | ACME | IND     Feb 2016 – Mar 2018"),
                    ("bullet", "Built the first.")])
    roles = [t for k, t in out if k == "role"]
    assert roles[0] == "SYSTEMS ANALYST / SENIOR PLATFORM ENGINEER, GLOBEX, USA     Mar 2024 – Present"
    assert roles[1].endswith("Apr 2018 – Dec 2022")                   # dates untouched


def test_titles_written_mostly_in_capitals_are_all_in_capitals():
    from ml.resume.writer import _roles_alike
    out = _roles_alike([("role", "Systems Analyst / SENIOR PLATFORM ENGINEER, Globex, USA\tMar 2024 – Present"),
                        ("role", "LEAD SITE RELIABILITY ENGINEER, CITY WTR. DEPT., IND\tJun 2019 – Nov 2023"),
                        ("role", "NETWORK Architect/ ENGINEER, CITY WTR. DEPT., IND\tJan 2017 – May 2019"),
                        ("role", "SYSTEMS ENGINEER, CITY WTR. DEPT., IND\tAug 2013 – Dec 2016")])
    assert out[0][1] == "SYSTEMS ANALYST / SENIOR PLATFORM ENGINEER, GLOBEX, USA\tMar 2024 – Present"
    assert out[2][1] == "NETWORK ARCHITECT / ENGINEER, CITY WTR. DEPT., IND\tJan 2017 – May 2019"
    mixed = [("role", "Senior Engineer, Acme\t2020 – 2022"), ("role", "Engineer, Initech\t2018 – 2020")]
    assert _roles_alike(mixed) == mixed                       # titles written in title case stay so
