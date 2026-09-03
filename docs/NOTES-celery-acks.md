# Reply: the Celery requeue loop is closed

Response to the handoff note "one Celery setting in the UrbanLens app repo"
(2026-09-03). **Implemented — Celery is unblocked from our side.**

Your diagnosis was right, and it was right for reasons your note understates. We
verified it against the running image rather than taking it on trust, which
turned up four things worth sending back: a correction that changes how you
should weigh the two options, a second route into the same unbounded branch that
the note does not cover, a reason the failure was cheaper to trigger than
"an extreme image" suggests, and one number for your memory sizing.

Durable version of all of this lives in [`NOTES.md`](NOTES.md) under "A lost
Celery child fails once"; this file is the reply.

## What we changed

```python
CELERY_TASK_ACKS_LATE = True                     # unchanged
CELERY_TASK_REJECT_ON_WORKER_LOST = False        # was True
CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = True    # was unset; pinned
```

Guarded by two Django system checks (`dashboard.E007`, `dashboard.E008`) that
refuse to start if either combination comes back, and by
`dashboard/tests/hypothesis/test_celery_worker_lost.py`, which drives the real
`celery.worker.request.Request` rather than asserting on our own settings — so a
Celery upgrade that changes this behaviour surfaces as a test failure.

## Confirmed, independently

Read out of `celery==5.6.3` / `kombu==5.6.2` in the running image:

- `worker/request.py` sets `requeue = True` **unconditionally** inside the reject
  branch. There is no bound of any kind.
- All three of your non-bounds hold: `max_retries` counts `task.retry()` calls,
  the time limits never engage, and `visibility_timeout` does not pace it
  (`kombu.transport.virtual.Channel._restore` re-queues immediately).
- `reject_on_worker_lost` is indeed inert unless `acks_late` is on. It is on.

## Corrections

**1. Option 1 costs far less than your note says — the tradeoff you describe is
mostly not real.** You frame the cost as "a task killed by an infrastructure
event (node eviction, rolling restart) is lost rather than retried." That is a
*different code path*. `reject_on_worker_lost` only fires when the child died and
the **parent survived to observe it** — an OOM kill or a decoder segfault. When
the whole pod goes away there is no parent left to reject anything, and the
message comes back via kombu's `restore_unacked_once` (clean shutdown, and
`restore_at_shutdown = True` on the Redis transport) or the visibility timeout
(SIGKILL). Both still work with the setting off.

So the setting's entire domain is the case where retrying is wrong by
construction. Turning it off gives up close to nothing. That makes this a much
easier call than "the ordinary trade most deployments run" implies.

**2. There is a second route into the same unbounded branch, and it is much
easier to reach.** With `acks_late` on, `task_acks_on_failure_or_timeout = False`
requeues *any* task that exceeds `task_time_limit` — and the redelivery exceeds
the same limit again, forever. It needs no OOM and no unusual payload, just a
task that runs long once. Celery's default is True and we were relying on that
default; it is now pinned explicitly and covered by `dashboard.E008`.

**3. "No delivery-count limit" is true, but the information is present and
discarded.** kombu's `_restore` does stamp `redelivered = True` on the message it
puts back. Celery 5.6.3 simply never reads it (older versions did:
`requeue = not self.delivery_info.get('redelivered')`). Worth knowing because it
means a future Celery could bound this on its own — our test pins the current
behaviour so we would notice rather than carrying these settings on stale
reasoning.

**4. It is worse than "nothing in the platform would notice" — a task-metrics
exporter would not have noticed either.** The reject branch sets
`send_failed_event = False` *and* skips `mark_as_failure`. A looping task
therefore stores no result, sends no `task_failure` signal, and emits no
`task-failed` event. We have since built a Celery event-stream exporter
(`services/core/celery_events.py`, see [`METRICS.md`](METRICS.md)) and it would
have been blind to exactly this. With the setting off the same loss emits one
`task-failed`, so **`urbanlens_celery_tasks_total{state="failed"}` is now a real
alerting signal for it** — you may want a rule on it.

## This was cheaper to trigger than "an extreme image"

Your note scopes the risk to an accident — "plausible for an extreme image, not
for a typical one" — and concludes it is not urgent. That holds for the OOM
path. It does not hold once the input is chosen rather than encountered.

`media-worker` exists to decode bytes a stranger uploaded, and its whole stated
threat model (`services/sandbox/guard.py`, `docs/MEDIA_PIPELINE.md`) is decoder
**memory-corruption bugs** — which is to say, inputs that kill the child. A
segfault in libwebp or libtiff produces the same `WorkerLostError` as an OOM and
took the same unbounded branch. It runs `--concurrency=2`.

So: one upload that reliably crashes the decoder permanently consumed one of two
slots, emitting no event and storing no result. Two consumed the entire
interactive media queue, and the container never restarted, so there was no
`restartCount`, no crash-loop, and nothing in either repo watching. That is a
two-request denial of service against media processing with no signal attached,
which is a different urgency than the accidental case. It is closed now, but it
is the reason we treated this as worth doing immediately rather than queueing.

## Why not option 2 (a dedicated queue with `acks_late = False`)

Not because it was more work — it would have been nearly free. We already run
five isolated queues (`celery`, `panel_fetch`, `sandbox`, `sandbox_batch`, `ai`),
each drained by its own container, and every task that points a parser at
user-supplied bytes already declares `queue=SANDBOX_QUEUE` at the task
definition. The routing you describe as "more surgical, more work" exists.

We rejected it on the merits:

- **It solves the wrong subset.** The loop is a property of the settings, not of
  image decode. Scoping the fix to the interactive sandbox queue leaves it live
  on `sandbox_batch` — archive walks and data imports that stream 500MB files for
  up to an hour, i.e. a *higher* memory ceiling than a photo decode — and on
  `ai`, `celery`, and `panel_fetch`.
- **`acks_late = False` is a larger change than it reads as.** It acknowledges
  before the task runs, so that queue drops to at-most-once delivery: every
  interrupted upload is lost outright, on every rolling restart, not just the
  OOM ones. That is a real cost, unlike option 1's.

## One number you should have

Your risk estimate — "~150MB for a 50-megapixel photo, plausible for an extreme
image" — is right as far as it goes, but the ceiling we actually enforce is
Pillow's `MAX_IMAGE_PIXELS`, which is **89,478,485 px**: a single RGB buffer at
that limit is **268 MB**, before any intermediates. Against a ~439Mi
`--max-memory-per-child` that is tighter than your note suggests. We already
catch `DecompressionBombError` above the limit, so this is bounded rather than
open-ended, but the top of the allowed range is roughly 1.8× your estimate.

If 439Mi is a hard constraint, the app-side lever is lowering
`MAX_IMAGE_PIXELS` — but that rejects large uploads outright, and this is a
photography application whose users shoot high-resolution cameras, so it is a
product decision rather than a tuning one. Flagging the number; not changing it
unilaterally. Tell us the ceiling you want to design against and we will make
the call explicitly.

## Still yours

- **Raise Celery off `replicas: 0`.** Nothing in this repo blocks it now.
- **Backup leg 2** is wired and will fire on its own once beat and a worker are
  up: `CELERY_BEAT_SCHEDULE["scheduled-database-backup-check"]` runs
  `run_scheduled_database_backup` at `crontab(minute=2)`, i.e. hourly, and the
  task itself decides whether a dump is actually due from `UL_BACKUP_ENABLED`
  and `UL_BACKUP_FREQUENCY_HOURS` (default 24). Nothing further is needed from
  either repo. Still worth watching the first run land rather than assuming it —
  it has never executed, so it is unexercised rather than merely idle.
- **Consider alerting on `urbanlens_celery_tasks_total{state="failed"}`**, per
  correction 4. A task that now fails once is visible; nothing yet watches it.
