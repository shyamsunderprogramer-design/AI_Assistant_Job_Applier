"""Bounded page-stability waits and per-run diagnostics for isolated browser checks."""
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def jobs_page_settled(page, still_ms=700, timeout_ms=5000):
    """Require the filter bar to stop moving; fail with evidence if it never settles.

    This is test synchronization, not a fix for the Jobs page's layout shift.
    Unlike the recovered scratchpad helper, the animation-frame loop is bounded.
    """
    return page.evaluate("""async ({still, timeout}) => {
      const bar = document.getElementById('q').closest('.bar');
      const at = () => Math.round(bar.getBoundingClientRect().top + scrollY);
      const first = at(), started = performance.now();
      let last = first, since = started;
      while (performance.now() - since < still) {
        if (performance.now() - started > timeout) throw new Error('Jobs page did not settle');
        await new Promise(resolve => setTimeout(resolve, 25));
        const now = at();
        if (now !== last) { last = now; since = performance.now(); }
      }
      return {first, last, moved: last - first, elapsed: performance.now() - started};
    }""", {"still": still_ms, "timeout": timeout_ms})


@contextmanager
def browser_diagnostics(page, tmp_path, label):
    """Never reuse a diagnostic directory, including for repeated/interrupted runs."""
    root = Path(os.environ.get("PHASE3_DIAGNOSTICS", str(tmp_path / "browser-diagnostics")))
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=label + "-", dir=root))
    with (folder / "events.jsonl").open("x") as log:
        def record(kind, **details):
            log.write(json.dumps({"kind": kind, **details}) + "\n")
            log.flush()
        listeners = {
            "pageerror": lambda error: record("pageerror", message=str(error)),
            "console": lambda msg: record("console", message=msg.text),
            "request": lambda req: record("request", method=req.method, url=req.url),
            "requestfailed": lambda req: record("requestfailed", url=req.url, error=req.failure),
            "response": lambda res: record("response", status=res.status, url=res.url),
        }
        for event, listener in listeners.items():
            page.on(event, listener)
        page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        record("started", label=label)
        try:
            yield folder
        except BaseException as exc:
            record("failed", error=repr(exc))
            raise
        else:
            record("passed")
        finally:
            try:
                page.screenshot(path=str(folder / "page.png"))
                (folder / "page.html").write_text(page.content())
            except Exception as exc:
                record("capture-failed", error=repr(exc))
            page.context.tracing.stop(path=str(folder / "trace.zip"))
            for event, listener in listeners.items():
                page.remove_listener(event, listener)
