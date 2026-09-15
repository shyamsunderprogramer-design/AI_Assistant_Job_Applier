"""The applicant's own answers, loaded once and checked before any form opens.

Everything here is a fact the person would type themselves. Nothing is
inferred, generated or defaulted into an answer — a blank field is left blank
on the form rather than filled with a guess, because on an application form a
guess is a claim.

Two groups get special treatment, and both for the same reason: they are
declarations rather than details.

`authorisation` (work authorisation, sponsorship) is a legal statement. An
employer relies on it, and a wrong answer is a misrepresentation rather than a
typo. So it has no default: if it is unset, `check_ready` stops the run and
says so, instead of picking the common answer.

`demographics` default to declining, which is a complete and lawful answer in
the US. Not because declining is better than answering, but because it is the
only default that does not answer on someone's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = "config/applicant.yaml"

# Questions no automation may answer from a template, however it is phrased.
# Matched loosely against a form's own label text.
DECLARATION_PATTERNS = (
    "sponsor", "authorized to work", "authorised to work", "work authorization",
    "work authorisation", "legally authorized", "legally authorised",
    "security clearance", "clearance", "export control", "itar",
)


class ProfileIncomplete(RuntimeError):
    """The profile cannot answer something an application will ask."""


@dataclass
class Applicant:
    """One person's answers, as the form filler needs them."""

    personal: dict = field(default_factory=dict)
    location: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    authorisation: dict = field(default_factory=dict)
    employment: dict = field(default_factory=dict)
    preferences: dict = field(default_factory=dict)
    demographics: dict = field(default_factory=dict)
    apply: dict = field(default_factory=dict)
    path: str | None = None

    # -- the handful of fields every form wants ----------------------------
    @property
    def first_name(self) -> str:
        return str(self.personal.get("first_name") or "").strip()

    @property
    def last_name(self) -> str:
        return str(self.personal.get("last_name") or "").strip()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def email(self) -> str:
        return str(self.personal.get("email") or "").strip()

    @property
    def phone(self) -> str:
        return str(self.personal.get("phone") or "").strip()

    @property
    def stop_before_submit(self) -> bool:
        # Defaults to stopping. A setting that has to be turned ON to submit
        # is the right way round for an irreversible action.
        return bool(self.apply.get("stop_before_submit", True))

    @property
    def max_per_day(self) -> int:
        try:
            return max(0, int(self.apply.get("max_per_day", 10)))
        except (TypeError, ValueError):
            return 10

    def answer_for(self, label: str) -> str | None:
        """The answer to one of the form's own questions, or None to leave blank.

        Only questions this profile genuinely covers get an answer. Anything
        else returns None and the field is left for the person — an
        approximately-right answer on an application is worse than an empty
        one, because it cannot be told apart from a considered one.
        """
        text = (label or "").strip().lower()
        if not text:
            return None

        if "linkedin" in text:
            return str(self.links.get("linkedin") or "").strip() or None
        if "github" in text:
            return str(self.links.get("github") or "").strip() or None
        if "portfolio" in text or "website" in text or "personal site" in text:
            return str(self.links.get("portfolio") or "").strip() or None

        if "sponsor" in text:
            needs = self.authorisation.get("requires_sponsorship")
            return None if needs is None else ("Yes" if needs else "No")
        if "authorized to work" in text or "authorised to work" in text \
                or "legally authorized" in text or "legally authorised" in text:
            allowed = self.authorisation.get("authorised_to_work")
            return None if allowed is None else ("Yes" if allowed else "No")
        if "clearance" in text:
            value = self.authorisation.get("security_clearance")
            return str(value).strip() if value else None

        if "how did you hear" in text or "referral source" in text:
            return str(self.preferences.get("referral_source") or "").strip() or None
        if "notice period" in text:
            return str(self.employment.get("notice_period") or "").strip() or None
        if "desired salary" in text or "salary expectation" in text:
            return str(self.employment.get("desired_salary") or "").strip() or None
        if "start date" in text or "available to start" in text:
            return str(self.employment.get("earliest_start_date") or "").strip() or None
        if "relocate" in text:
            willing = self.employment.get("willing_to_relocate")
            return None if willing is None else ("Yes" if willing else "No")

        return None


def load(path=None) -> Applicant:
    """Read the applicant profile, or say clearly that there isn't one."""
    import yaml

    target = Path(path or DEFAULT_PATH)
    if not target.exists():
        raise ProfileIncomplete(
            f"No applicant profile at {target}.\n"
            f"  Create one:  cp config/applicant.yaml.example {target}\n"
            f"  then fill it in. It is gitignored, so it stays on this machine."
        )
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return Applicant(
        personal=data.get("personal") or {},
        location=data.get("location") or {},
        links=data.get("links") or {},
        authorisation=data.get("authorisation") or {},
        employment=data.get("employment") or {},
        preferences=data.get("preferences") or {},
        demographics=data.get("demographics") or {},
        apply=data.get("apply") or {},
        path=str(target),
    )


def check_ready(applicant: Applicant) -> list[str]:
    """Everything that would stop an application, as a list of plain sentences.

    Returned rather than raised so a caller can show all the problems at once.
    An empty list means the profile can fill a form.
    """
    problems: list[str] = []

    for label, value in (("first name", applicant.first_name),
                         ("last name", applicant.last_name),
                         ("email", applicant.email),
                         ("phone", applicant.phone)):
        if not value:
            problems.append(f"{label} is blank — every form asks for it")

    if "@" not in applicant.email or "." not in applicant.email.split("@")[-1]:
        if applicant.email:
            problems.append(f"{applicant.email!r} does not look like an email address")

    # The declarations. No default is possible, so an unset value is a stop.
    if applicant.authorisation.get("authorised_to_work") is None:
        problems.append(
            "authorisation.authorised_to_work is unset. This is a legal "
            "declaration on a real application and will not be guessed — "
            "set it to true or false.")
    if applicant.authorisation.get("requires_sponsorship") is None:
        problems.append(
            "authorisation.requires_sponsorship is unset. Same reason — "
            "set it to true or false.")

    return problems
