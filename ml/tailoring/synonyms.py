"""Names for the same skill, things that count as it, and things only related to it.

Three different relationships, kept apart on purpose:

  SAME      Different names for one thing: "K8s" is Kubernetes. A match on
            any of them is a direct match.
  COUNTS    Doing one is hands-on use of the other: running EKS is running
            Kubernetes. Direct as well, and the explanation says why.
  RELATED   Comparable, not the same: GitLab CI and Jenkins are both CI
            servers. A related match at most -- experience with a similar
            technology is never presented as experience with the exact one.

Every RELATED group carries the reason the items are considered related,
which is shown in the match report.
"""

from __future__ import annotations

import re

SAME: dict[str, tuple[str, ...]] = {
    "kubernetes": ("k8s", "kube"),
    "amazon web services": ("aws",),
    "google cloud": ("gcp", "google cloud platform"),
    "microsoft azure": ("azure",),
    "ci/cd": ("cicd", "ci cd", "continuous integration", "continuous delivery", "continuous deployment"),
    "infrastructure as code": ("iac",),
    "postgresql": ("postgres", "psql"),
    "javascript": ("js", "ecmascript"),
    "typescript": ("ts",),
    "node.js": ("nodejs", "node js"),
    "golang": ("go lang",),
    "github actions": ("gh actions",),
    "azure devops": ("ado", "vsts"),
    "site reliability engineering": ("sre",),
    "machine learning": ("ml",),
    "amazon eks": ("eks", "elastic kubernetes service"),
    "azure aks": ("aks",),
    "google gke": ("gke",),
    "amazon ec2": ("ec2",),
    "amazon s3": ("s3",),
    "argo cd": ("argocd", "argo-cd"),
    "elasticsearch": ("elastic search",),
    "elk": ("elk stack", "elastic stack"),
    "gitops": ("git ops",),
    "devsecops": ("dev sec ops",),
    "restful apis": ("rest api", "rest apis", "restful api", "restful web services", "rest services"),
    "microservices": ("micro services", "micro-services"),
    "java": ("jdk",),
    "spring boot": ("springboot",),
    "docker": ("docker containers",),
    "linux": ("rhel", "red hat enterprise linux", "ubuntu", "centos"),
}

# Doing the first is doing the second. A list: EKS counts as Kubernetes AND as AWS.
COUNTS: list[tuple[str, str, str]] = [
    ("amazon eks", "kubernetes", "EKS is a managed Kubernetes service; running it is running Kubernetes."),
    ("azure aks", "kubernetes", "AKS is a managed Kubernetes service; running it is running Kubernetes."),
    ("google gke", "kubernetes", "GKE is a managed Kubernetes service; running it is running Kubernetes."),
    ("openshift", "kubernetes", "OpenShift is built on Kubernetes; running it is running Kubernetes."),
    ("helm", "kubernetes", "Helm deploys to Kubernetes clusters."),
    ("github actions", "ci/cd", "GitHub Actions is a CI/CD system."),
    ("gitlab ci", "ci/cd", "GitLab CI is a CI/CD system."),
    ("jenkins", "ci/cd", "Jenkins is a CI/CD server."),
    ("azure devops", "ci/cd", "Azure DevOps Pipelines is a CI/CD system."),
    ("terraform", "infrastructure as code", "Terraform is an infrastructure-as-code tool."),
    ("cloudformation", "infrastructure as code", "CloudFormation is an infrastructure-as-code tool."),
    ("pulumi", "infrastructure as code", "Pulumi is an infrastructure-as-code tool."),
    ("arm templates", "infrastructure as code", "ARM templates are infrastructure as code for Azure."),
    ("argo cd", "gitops", "Argo CD is a GitOps delivery tool."),
    ("fluxcd", "gitops", "Flux is a GitOps delivery tool."),
    ("amazon ec2", "amazon web services", "EC2 is an AWS service."),
    ("amazon s3", "amazon web services", "S3 is an AWS service."),
    ("amazon eks", "amazon web services", "EKS is an AWS service."),
]

RELATED: list[tuple[set[str], str]] = [
    ({"jenkins", "gitlab ci", "github actions", "circleci", "azure devops", "teamcity", "bamboo", "tekton"},
     "CI/CD servers that do the same job"),
    ({"amazon web services", "microsoft azure", "google cloud", "oracle cloud"},
     "public cloud platforms with comparable services"),
    ({"terraform", "cloudformation", "pulumi", "arm templates", "bicep", "opentofu"},
     "infrastructure-as-code tools"),
    ({"ansible", "chef", "puppet", "saltstack"}, "configuration management tools"),
    ({"prometheus", "datadog", "new relic", "dynatrace", "splunk", "grafana", "cloudwatch", "elk"},
     "monitoring and observability tools"),
    ({"kubernetes", "openshift", "docker swarm", "nomad", "amazon ecs"}, "container orchestrators"),
    ({"docker", "podman", "containerd"}, "container runtimes"),
    ({"postgresql", "mysql", "sql server", "oracle database", "mariadb"}, "relational databases"),
    ({"mongodb", "dynamodb", "cassandra", "couchbase", "cosmos db"}, "NoSQL databases"),
    ({"kafka", "rabbitmq", "amazon sqs", "kinesis", "pub/sub", "activemq"}, "messaging and streaming systems"),
    ({"react", "angular", "vue"}, "front-end frameworks"),
    ({"java", "kotlin", "scala"}, "JVM languages"),
    ({"argo cd", "fluxcd", "spinnaker"}, "continuous deployment tools"),
    ({"vmware", "hyper-v", "kvm", "openstack"}, "virtualisation platforms"),
    ({"hashicorp vault", "aws secrets manager", "azure key vault"}, "secrets management tools"),
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w/+#. -]", " ", (text or "").lower())).strip()


_TO_CANON: dict[str, str] = {}
for canon, names in SAME.items():
    _TO_CANON[canon] = canon
    for n in names:
        _TO_CANON[n] = canon


def canonical(term: str) -> str:
    t = _norm(term)
    return _TO_CANON.get(t, t)


def names_for(term: str) -> list[str]:
    """Every way the resume might write this term, the canonical one first."""
    c = canonical(term)
    return [c, *SAME.get(c, ())]


# Names that are ordinary words or letters in other casing: matched exactly as
# written. "GO." in an employer's name is not the Go language.
EXACT = {"go": ("Go", "Golang", "golang"), "r": ("R",), "c": ("C",), "rust": ("Rust",),
         "swift": ("Swift",), "chef": ("Chef",), "puppet": ("Puppet",), "salt": ("Salt", "SaltStack")}


def mentions(text: str, term: str) -> bool:
    """Does `text` name `term` (under any of its names), as a whole word?"""
    exact = EXACT.get(canonical(term))
    if exact:
        return any(re.search(r"(?<![\w.])" + re.escape(n) + r"(?![\w])(?!\.)", text or "") for n in exact)
    low = _norm(text)
    for name in names_for(term):
        if re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", low):
            return True
    return False


def implied_by(text: str, term: str) -> str | None:
    """A reason `text` demonstrates `term` through something that counts as it, else None."""
    target = canonical(term)
    for source, implied, why in COUNTS:
        if implied == target and mentions(text, source):
            return why
    return None


def related_reason(text: str, term: str) -> tuple[str, str] | None:
    """(the related thing the text names, why it is related) -- never the term itself."""
    target = canonical(term)
    for group, why in RELATED:
        if target in group:
            for other in sorted(group - {target}):
                if mentions(text, other):
                    return other, f"{other} and {target} are both {why}"
    return None
