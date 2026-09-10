# The WSGI tier's worker model: an undocumented choice and an unexercised failure mode

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R28 — The WSGI tier runs gevent with no recorded rationale, contradicting reasoning the project applied everywhere else it chose a worker model

`id: R28` · `status: current` · `updated: 2026-09-10`

This documents the current state and an internal contradiction for a human to resolve. It is not a
recommendation, and no decision is recorded here — see P104 for the concrete, already-manifested
risk this leaves open.

**Origin.** `gunicorn -k gevent` (`package.json:16`, the `start` script) enters at `549c22537`
(2026-07-04, "Clean Git History. Release v0.2.0-alpha"), an empty commit body; its parent ran plain
sync gunicorn. `daphne` (Channels' ASGI server) arrives separately at `ad4933141` (2026-07-06),
whose body reads only "Channels for Real-time chat." Neither commit records why that worker class,
specifically, was chosen. `uvicorn` was never evaluated anywhere in the git history searched this
session.

**The project decided against gevent for CPU-bound work twice elsewhere, using reasoning never
applied to the WSGI tier.** `bf81fb219` (2026-07-15) put external-panel-fetch tasks on a dedicated
`celery-worker-panels` queue with `--pool=threads --concurrency=20`, and its commit body states the
reasoning explicitly: "Deliberately threads over gevent/eventlet... a CPU-bound unit of work never
yields in a gevent loop and would wedge every task sharing that loop, whereas the GIL still
preempts between real OS threads every few ms." `src/urbanlens_ai/wsgi.py:5-8` reaches the same
conclusion independently, for the AI-inference service: deployed behind gunicorn's `gthread`
worker "not gevent: the provider SDKs' blocking HTTP calls need a worker model that actually parks
on I/O." Neither decision's reasoning is cited against, or reconciled with, the `app` service's own
`-k gevent` — which is exactly where this investigation found CPU-bound work (63,240 Python object
constructions per map-payload request before the fix; see R27) actually landing.

**Channels does not need the WSGI tier to be gevent.** `config/nginx/django.conf:49-60` routes `/`
to `gunicorn` (the `app` service) only; `config/nginx/django.conf:67-79` (with its own comment at
`:63-64`, "the main `app` service above never sees WebSocket traffic") routes `/ws/` to the separate
`app-ws` `daphne` service only — the two never share a socket or a worker. gevent is not merely
unnecessary for Channels here, it is actively hostile to it: `services/core/channel_broadcast.py`
exists solely to route every `async_to_sync(channel_layer.group_send)` call through a Celery hop,
because running that call inline on a gevent worker corrupted unrelated concurrent requests'
`SynchronousOnlyOperation` check (`docs/archive/PROBLEMS-ARCHIVE.md:1760`, resolved 2026-07-31 by
adding that hop). That archived entry itself records, at the time, "Deliberately not done:
switching the WSGI worker off gevent entirely... considered... but [a] larger architecture change"
— a decision to defer, not a decision to keep gevent on its merits.

**Open, not decided**: whether the `app` service should move off gevent (to threads or sync
workers, per the reasoning the project already applied to `celery-worker-panels` and
`urbanlens_ai`), and if so what replaces the async-request-corruption protection
`channel_broadcast` currently provides. Left for a human decision; this entry exists so the next
person making it has the contradiction and its evidence in one place rather than re-discovering it.

## N14 — Nothing in pytest or local dev exercises the shared-connection-pool topology that caused P104's outage

`id: N14` · `status: current` · `updated: 2026-09-10`

Complements `docs/TOOLING.md`'s own "Load testing (Locust / k6)" gap note (`TOOLING.md:632-634`,
R13) with the specific reason this particular failure mode (P104) cannot surface short of one:
`src/bin/init.py:604-607` runs `manage.py runserver` in `development`
(`init.py:484-492`, `run_dev_server`) and only reaches gunicorn/gevent in `staging`/`production`
(`init.py:494-502`, `run_prod_server`, which shells out to `bun run start`). `settings/test.py`
swaps Redis for `LocMemCache` (`test.py:65`) and Celery's broker/result-backend for the in-process
`memory://`/`cache+memory://` transport (`test.py:82-83`) under test.

So neither a local dev run nor the pytest suite ever constructs the shared-connection-pool topology
— gunicorn workers with gevent greenlets, daphne, and two Celery workers, all connecting as the
same Postgres role with no pooler — that produced the 11-hour outage in P104. Proving or disproving
that availability invariant needs a load probe against a real multi-container deployment, not
another unit test.
