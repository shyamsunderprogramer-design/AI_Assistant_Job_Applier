

def test_a_posting_with_no_blank_lines_keeps_everything_but_its_boilerplate_lines():
    from ml.resume.ats import requirements_only
    jd = ("Senior DevOps Engineer\n\nThe Team\nWe build secure platforms.\nResponsibilities\n"
          "- Build CI/CD pipelines with Terraform and GitHub Actions\n- Automate FedRAMP evidence collection\n"
          "Qualifications\n- 8+ years of DevOps experience\n- Kubernetes and GCP\n"
          "Salary\nThe base salary range is $150,000-$190,000.\nBenefits\nMedical, dental and a 401(k).")
    kept = requirements_only(jd)
    assert "Terraform and GitHub Actions" in kept and "Kubernetes and GCP" in kept
    assert "$150,000" not in kept and "401(k)" not in kept
