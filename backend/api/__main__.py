from backend.api.app import cfg, main
from backend.api.morning_pages import watch_mornings
from backend.apply.inbox import watch

# Email about applications, looked at every half hour while the app runs (backend/apply/inbox.py).
# Started here, not on import, so tests and one-off scripts never open the mailbox.
watch(cfg)
# A morning run the Mac missed while it was off, started once it is on (backend/api/morning_pages.py).
watch_mornings()
# The chosen jobs a crash or a restart left mid-way: forms not yet open back in the queue, forms
# that were open waiting for the person's word on whether they went (backend/apply/execution.py).
try:
    from backend.apply.execution import recover
    cfg()
    recover()
except Exception as exc:                 # never a reason for the app not to start
    print(f"Chosen jobs not checked after the restart: {exc}")

raise SystemExit(main())
