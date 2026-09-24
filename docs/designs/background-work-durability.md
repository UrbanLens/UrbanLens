# D21 — Background work survives a refused enqueue, a dead worker and a swallowed soft limit, through one mechanism each

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D21` · `status: accepted` · `updated: 2026-09-24` · `asked for by: grok audit N29, theme T6 (G3-29, G3-34, G3-38, G4-7, G4-19, G5-23, G6-14, G3-8, G5-9, G5-18)` · `code: src/urbanlens/dashboard/services/core/{celery,task_outbox,task_limits,retention}.py`

## What was wrong

- `safely_enqueue_task` returned `None` when the broker refused a message, and about 75 of its 104
  callers ignored it. Scan enqueues after upload, WhatsApp/SMS alerts, calendar pushes, friend
  invitations and fact recomputes were lost until a slow sweep noticed (six hours for uploads) or
  for ever (everything else).
- Some tasks claimed their row before doing the work (`process_device_scan_upload` flipped
  PENDING straight to PROCESSED), so a worker that died mid-run lost the work for good.
- Almost every task inherited the global 2700/3600-second limits, so an interactive task could hold
  a slot the safety escalations share for 45 minutes, and hourly sweeps held 3300-second locks that
  could expire mid-run.
- Billiard's `SoftTimeLimitExceeded` is an `Exception`. The codebase has about 246 broad
  `except Exception` handlers (`grep -rnE 'except (Exception|BaseException)\b' src/urbanlens`,
  2026-09-24, excluding tests and migrations), so a soft limit was usually swallowed and the task
  ran on to the hard kill.

## What we chose

1. **A failure-only transactional outbox at the one enqueue chokepoint.** `safely_enqueue_task`
   is the only place in the codebase that calls `apply_async` (checked with
   `grep -rn 'apply_async\|\.delay(' src/urbanlens`). A refused `durable=True` enqueue (the
   default) writes a `TaskOutboxEntry` in the caller's transaction, and `drain_task_outbox`
   re-queues it every minute. A caller that handles `None` itself passes `durable=False`, and
   `bin/check_enqueue_durability.py` makes any caller that uses the result choose.
   - *Rejected: write every enqueue to the outbox and drain it on commit.* It would cost two
     writes per enqueue on the healthy path, including every photo upload, to cover a crash in the
     microseconds between commit and publish.
   - *Rejected: one dirty flag per model.* It covers only the sites someone remembers. Flags are
     still used where the task itself can fail after it runs (a Google write for calendar push,
     evidence landing mid-recompute for facts), because only a domain marker says the work is
     still owed.
2. **Claim-then-work-in-one-transaction plus a stall sweep** for tasks that must not repeat a side
   effect: the claim (`PROCESSING`, `claimed_at`, `attempts`) commits alone, then the work and the
   flip to done commit together. A dead worker's partial work rolls back, and the sweep hands the
   row to another worker, giving up after three claims.
3. **A default per queue, a ceiling per queue, and a startup check.** A `task_annotations` entry
   fills in the limits a decorator leaves unset. `dashboard.E013` refuses a task above its queue's
   ceiling, and caps every task below the broker visibility timeout.
4. **A soft limit that broad handlers cannot catch.** For the duration of each task,
   `UrbanLensTask` raises `TaskSoftTimeLimit` (a `BaseException`) from the soft-limit signal, and
   converts it to `SoftTimeLimitExceeded` at the task boundary so Celery records an ordinary
   failure.
   - *Rejected: add `except SoftTimeLimitExceeded: raise` before every broad handler.* 246 sites,
     and the next one written would be missed.

## What this does not cover

- The outbox covers a broker that refuses a message. It does not cover a message the broker
  accepted and then lost, or a process that dies between commit and publish; domain sweeps cover
  those where they matter.
- `TaskSoftTimeLimit` can still escape if the limit fires in the few bytecodes where
  `soft_limit_escapes_broad_handlers` restores billiard's handler. Billiard sends the signal once
  per job, so this needs the limit to expire in exactly that window. Not measured.
- `requests` timeouts limit each socket operation, not the whole call. The assistant's budget
  (`InferenceRequest.timeout_seconds`) therefore bounds the provider call inside `ai-inference`
  exactly, but bounds the HTTP hop to it only per read.
- The interactive overrides (240/270 s for tasks making AI or several upstream calls) were chosen
  by reading each task, not by measuring how long they run. Not measured.
