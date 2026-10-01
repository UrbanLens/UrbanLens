# dashboard/tests/ — Writing Tests Here

Applies to `src/urbanlens/dashboard/tests/`. pytest and Model Bakery; run with
`--reuse-db` and a unique `UL_TEST_DB_NAME`.

Test everything substantive. Do not write trivial tests — asserting a log
message's exact wording makes an unrelated edit fail the suite. Mock and patch
anything that leaves the process. Prefer hypothesis property tests where a
property exists, and `SimpleTestCase` wherever the test does not need the DB.

Ruff lints this directory, but only for mechanical hygiene: import order, quote
style, modernisation. It will not argue about a deliberately wrong type, a
private access, a fake password or a naive datetime. Test files are formatted at
120 columns, not the project's 250 — see `[tool.ruff.lint.per-file-ignores]`.

## Gotchas

- **Hypothesis `@given` with `self.client` works, but is slow** (changed 2026-07-27).
- **`UL_CELERY_TASK_ALWAYS_EAGER=True`** runs dispatched
  tasks *inline, inside the request*. Any test asserting a request did **not** do background work
  must stub `services.core.celery.safely_enqueue_task`, or it tests Celery's eager mode instead.
- **A fresh `CollectorRegistry` is not fresh values.** When
  `PROMETHEUS_MULTIPROC_DIR` is set (it is, in the app container), prometheus_client
  backs every sample with an mmap keyed by metric name and labels, shared by every
  registry in the process. Two tests that both touch the same metric and labels see
  each other's increments, so an absolute-value assertion passes or fails depending
  on what ran before it. Assert deltas against a baseline snapshot taken when the
  metric object is built - see `CeleryEventMetricsTests._metrics`.
- **`settings/test.py` cannot undo what `base.py` derived at import time.**
  `DJANGO_SETTINGS_MODULE=...settings.test` imports the *package* first, and its
  `__init__` already runs `from .base import *`; the later `from .base import *`
  re-exports the cached module rather than re-executing it. Anything base computes
  from the environment (`INSTALLED_APPS` entries, `MIDDLEWARE`, settings derived
  from another setting) has to be undone explicitly in `test.py`, and a setting read
  from the pydantic `app_settings` singleton has to be set on that object.
