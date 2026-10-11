---
status: accepted
date: 2026-09-24
---

# Background work survives a refused enqueue, a dead worker and a swallowed soft limit

Formerly `D23`.

Refused enqueues were silently lost, tasks that claimed work before doing it lost it when a worker died, and broad `except Exception` handlers swallowed Celery's soft time limit. Each failure gets one mechanism. A refused enqueue at the single chokepoint `safely_enqueue_task` writes a failure-only outbox row that a periodic task re-queues. Tasks whose side effect must not repeat commit their claim alone, then do the work and mark it done in one transaction, with a stall sweep that hands a dead worker's row to another. Each queue has a default and a ceiling time limit, checked at startup. The soft limit is raised as `TaskSoftTimeLimit`, a `BaseException`.

## Considered options

- Write every enqueue to the outbox and drain on commit: two writes per enqueue on the healthy path, to cover a microsecond window.
- A dirty flag per model: covers only the sites someone remembers. Kept only where the task can fail after it runs.
- `except SoftTimeLimitExceeded: raise` before each broad handler: about 246 sites, and the next one would be missed.

## Consequences

- The outbox does not cover a message the broker accepted and then lost, or a crash between commit and publish; domain sweeps cover those where they matter.
