"""Process-local hold for the web app's two automatic startup watchers.

Set JOB_APPLIER_HOLD_STARTUP_WATCHERS=1 in the new web process environment.
This does not stop existing threads, change saved settings, or control launchd jobs.
"""

import logging
import os

HOLD_ENV = "JOB_APPLIER_HOLD_STARTUP_WATCHERS"


def watcher_startup_held(name: str) -> bool:
    if os.environ.get(HOLD_ENV) != "1":
        return False
    logging.getLogger(__name__).warning(
        "Startup watcher %s held by %s=1; no thread or timer started", name, HOLD_ENV)
    return True
