- **Hypothesis `@given` with `self.client` works, but is slow** (changed 2026-07-27).
- **`UL_CELERY_TASK_ALWAYS_EAGER=True`** runs dispatched
  tasks *inline, inside the request*. Any test asserting a request did **not** do background work
  must stub `services.core.celery.safely_enqueue_task`, or it tests Celery's eager mode instead.