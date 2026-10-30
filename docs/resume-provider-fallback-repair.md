# Resume provider fallback repair

## Defect and scope

When Claude resume generation failed, `_dispatch` selected the primary provider's
model again for ChatGPT and Gemini. With Claude's default selection this supplied
`opus` to both other CLIs. The reverse fallback to Claude also inherited the
primary provider's model. The recorded live warnings discarded the individual
errors, so the exact cause of that historical run's failures remains unconfirmed.

## Changes

- `ml/resume/llm.py`: resolve model names for the actual destination provider.
  `RESUME_MODEL` continues to override the primary provider only. Backups use
  their own configured model or existing default; ChatGPT/Gemini defaults omit
  an explicit model argument through their existing CLI adapters.
- Log the failing provider and a safe failure category, followed by the next
  provider being tried. Do not print raw CLI output, which may echo private input.
  Unknown failures are explicitly described as unclassified.
- `qa/tests/test_llm_fallback_models.py`: regression coverage for switches in both
  directions, destination-specific configuration, primary environment overrides,
  the default Claude model, and failure reporting without private output.

No provider order, fallback permission, timeout, submission gate, application
record, configuration, or database schema was changed. The existing 900-second
Ollama timeout can still cause a long wait if every plan fallback fails.

## Verification

Before the repair, the initial regression tests produced **6 failures and 2
passes**, reproducing model leakage in both directions. After the repair:

```text
.venv/bin/python -m pytest -q -p no:cacheprovider \
  qa/tests/test_llm_fallback_models.py qa/tests/test_llm_fallback.py \
  qa/tests/test_llm.py qa/tests/test_llm_providers.py \
  qa/tests/test_llm_claude_code.py qa/tests/test_plans.py
60 passed
```

Provider generation calls were mocked. Existing test fixtures prohibit real AI
CLIs, external connections and production database access, and check protected
application-file stamps. No live generation or application submission was used
to verify this repair. The full application suite was not rerun for this change.

## Activation

No web restart or worker interruption was performed. The job-page route starts
a new `main.py apply` subprocess via `start_task`; a future newly started worker
will import this patch. An already running worker retains its loaded code.
This source change does not establish live provider availability or complete
the separate pending Phase 3 activation verification.
