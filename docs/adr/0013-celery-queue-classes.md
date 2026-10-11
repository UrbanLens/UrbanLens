---
status: accepted
date: 2026-09-11
---

# Celery tasks are classed interactive, bulk or maintenance, on separate workers

Formerly `D13`.

87 of 96 tasks shared the default queue and four worker slots, so one account's library sweep could delay a safety check-in escalation. Every task now declares a queue class: `INTERACTIVE` (someone is waiting, or a deadline is safety-critical), `BULK` (one account's large job), or `MAINTENANCE` (beat-driven, site-wide). A startup check fails on any task with no queue. Safety check-in tasks are interactive even though beat drives them. This extends the existing sandbox/sandbox-batch split. The classification is an agent's, built but not reviewed by Jess.

## Consequences

- One new worker, not two: `celery-worker-bulk` drains bulk, maintenance and the default `celery` queue at low priority, so a task with a forgotten queue still runs, slowly.
- It bounds concurrency, not how many bulk jobs one account queues over time.
