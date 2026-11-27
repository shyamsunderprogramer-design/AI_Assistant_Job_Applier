"""Which failures count toward retiring a board (limits.deactivate_after_failures in a row). No dependencies: the
merge (devops/discovery_merge.py) reads it too, on a runner without the scraper's packages."""

from __future__ import annotations

import re

# Failures that say nothing about the board itself (27 Nov 2026): a refusal (429), no answer, the host's own trouble
# (5xx through every retry), a robots.txt that cannot be read now -- and the owner's robots.txt rules, which are the
# owner's choice for now, reported as owner-restricted and checked again each run, never a reason to drop the board.
NOT_ABOUT_THE_BOARD = ("RateLimited", "NetworkError", "ServerError", "RobotsUnavailable", "RobotsDisallowed")


def counts_toward_retiring(error_type: str, message: str = "") -> bool:
    """Whether a failure counts toward retiring a board (limits.deactivate_after_failures in a row). Also reads the
    older log's messages, from before ServerError/RateLimited existed: "... after 3 retries: HTTP 429/5xx"."""
    if error_type in NOT_ABOUT_THE_BOARD:
        return False
    text = message or ""
    return not re.search(r"after \d+ retries: HTTP (429|5\d\d)\b", text)
