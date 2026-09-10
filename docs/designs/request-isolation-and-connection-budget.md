# D11 — One user's expensive request must be unable to reach another user's request, and the way to guarantee that is bounded pools with named budgets

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D11` · `status: accepted` · `updated: 2026-09-10`

Supersedes the open question in R28 (`docs/notes/wsgi-worker-model-and-connections.md`) and is the
fix P104 was waiting on a decision for.

The requirement, in Jess's words: *"No action a user takes should impact the availability of the
site for other users, ever."* R27's map-payload fix made the expensive path cheap. That is not the
same guarantee — it removed one way to spend a worker, and left the mechanism intact. This decides
the mechanism.

## Decision 1: the WSGI tier moves from `gevent` to `gthread`, 3 workers × 4 threads

Verified in gunicorn 26.2.0's own source, because the three worker classes differ in a way that
decides this:

- `gthread.run()` calls `self.notify()` on the main thread each loop (`workers/gthread.py:409-419`),
  so a CPU-bound request thread never stops the heartbeat and `-t` never reaps it.
- `sync.run_for_one()` notifies from the same thread that serves the request
  (`workers/sync.py:58-61`), so there `-t` *is* a genuine per-request deadline.
- gevent's notify is a greenlet in the request thread. A pure-Python section stops it, the arbiter
  declares the worker dead (`arbiter.py:604-623`), and SIGKILL takes **every other in-flight request
  on that worker** with it. One user's slow request does not merely queue behind — it kills.

That last line is the whole argument. Under gevent the failure mode is collateral by construction,
and `worker_connections` defaults to 1000 (gunicorn's own `gunicorn/config.py`, not ours), so with `CONN_MAX_AGE=0` a single
worker can demand up to 1000 Postgres backends and three can demand 3000 against
`max_connections=100`. Under `gthread` the in-process concurrency, and therefore the connection
demand, is `workers × threads` — a number chosen rather than discovered. 3 × 4 = 12.

This is the reasoning the project already recorded twice and never applied here: `celery-worker-panels`
chose `--pool=threads` over gevent (commit `bf81fb219`) citing that gevent never yields and wedges
every task in the loop, and `urbanlens_ai` chose `gthread` for the same reason. `-k gevent` entered
the WSGI tier at `549c22537` with an empty commit body and no record. Channels is not a
counter-argument: nginx routes `/ws/` to daphne (`app-ws`) and everything else to `app`, so the WSGI
tier has no async protocol to serve.

Consequences taken deliberately:

- Real threads make `CONN_MAX_AGE=300` + `CONN_HEALTH_CHECKS` safe and useful (under gevent a
  persistent connection is greenlet-local and simply leaks), removing a backend fork per request.
- `psycogreen` is applied only when the worker class is actually gevent, so the rollback is one
  environment variable rather than a revert.
- With `cpus: 2` and the GIL, each process gets at most one core of Python. That is already the
  effective ceiling; threads do not make it worse, and they stop one request from monopolising a
  whole worker's request slots.
- **`-t` stops being a request bound.** Nothing in the light tier reaps a runaway request any more.
  That is intentional — the bound comes from decision 2 (heavy work is routed to a pool where `-t`
  works), decision 3 (`statement_timeout` per role), and bounding the work itself. The nginx comment
  and the `f2623a5d4` commit message that read as if `-t` bounds an abandoned request are wrong under
  either class and must be rewritten.

Rejected: capping `--worker-connections` and staying on gevent. It bounds connections — and it is
worth shipping *immediately* as an interim, because it is one flag — but it leaves the
kill-the-whole-worker failure mode, `psycogreen`, and the `post_worker_init` ordering hazard in place.

### Measured 2026-09-10, and it qualifies the argument above

The interim was shipped and then run under load on a staging-model environment (X15). Two of these
claims now have measurements rather than source reading behind them, and they do not agree with each
other.

**The cap works exactly as specified.** Peak **61/100 backends, 60 of them web, 0% idle** during a
60-user filter storm — `--worker-connections 20 × 3 workers`, to the connection. P104's mechanism
cannot recur while the flag is set.

**The kill-the-whole-worker failure mode did not occur.** Zero `WORKER TIMEOUT`, zero SIGKILL, across
a 60-user filter storm *and* the most CPU-bound phase in the suite (four concurrent whole-account
serialisations). The source reading is not wrong — the arbiter does kill a worker whose heartbeat
stops — but the threshold matters and was never stated: starving the heartbeat for `-t 180`
consecutive seconds requires a request that *runs* for 180 seconds. Ordinary heavy endpoints cost
seconds, and the heartbeat is scheduled between them.

Two known requests can run that long, and both have entries: **P108** (a 20,000-pin map page spends
~7 minutes in a pairwise Haversine scan) and **P96** (a 20,000-pin import would run 78 minutes). So
the collateral-kill argument is a property of those two defects, not of heavy endpoints in general.
That is a materially weaker case for the move than this section originally made, and it should be
weighed against P108 and P96 being fixable directly.

**What the measurement did *not* weaken is decision 2.** The cap bounds the database's exposure and
provides no fairness at all between users: during the storm the neighbour's *median* request did not
complete and 94.8% of its requests could not be started, because all 60 slots belonged to one
account. Whatever happens to the worker class, something has to stop one user owning the whole
request pool.

## Decision 2: a second gunicorn pool, `app-heavy`, owns the endpoints that can legitimately cost seconds

`sync` workers, own `cpus`/`mem_limit`, own Postgres role, own nginx `location` with
`limit_conn 2` per session and `6` total answering `429`. Chosen because `sync` gives back the
per-request reaper that `gthread` gives up, and because one request per process means two heavy
requests never share a GIL.

The membership list is data, not code: `src/urbanlens/config/heavy_routes.txt`, one regex per line,
rendered by `bin/render_heavy_routes.py` into both the nginx block and the cloudflared path rules,
with a pre-commit staleness check. Adding an endpoint to the heavy pool is a one-line change and no
Python is aware of which pool serves it. As P96, P98, P102 and P2 move their work to Celery, their
routes *leave* the file — the list shrinking is the visible measure of that programme.

Rejected alternatives, and why each is a complement rather than a substitute:

- **`limit_req` per user alone.** Bounds one user's share, not the class; twelve users each inside
  their own limit still saturate the tier. Kept alongside.
- **A per-request work budget in code.** Cannot preempt CPU already running, and it is per-endpoint
  work forever.
- **Move everything to Celery + polling.** Right for imports, label fan-out, the admin panel, and
  parsing. Wrong for the map filter, where the user is waiting on the answer and a job queue adds a
  round trip to every filter change.

## Decision 3: per-role Postgres users with `CONNECTION LIMIT`, not a pooler — yet

The outage read 97/100 connections *idle*, which is exactly
`max_connections - superuser_reserved_connections`. Idle, under `CONN_MAX_AGE=0`, means a connection
object still alive in some process. Every container connects as the same superuser, so the postmortem
could not say which tier held them, and the 3 reserved slots protected nothing — the superuser is
what the app connects as.

Ten login roles inheriting one grant-holding group, with limits summing to 73 of 97 usable
(`ul_web` 20, `ul_panels` 20, `ul_web_heavy` 6, `ul_worker` 6, `ul_ws`/`ul_bulk`/`ul_maintenance`/
`ul_sandbox` 4, `ul_ai` 3, `ul_beat` 2), plus `statement_timeout`,
`idle_in_transaction_session_timeout`, `idle_session_timeout` and `lock_timeout` per role via
`ALTER ROLE ... SET`. Applied by an idempotent one-shot compose service on every `up`, so changing a
budget is a config edit; not `initdb` (runs only on an empty volume, so it can never reach the live
one) and not a migration (migrations run as the app role and cannot `CREATE ROLE`, and passwords do
not belong in migration history). Migrations keep the owner role, so no `statement_timeout` ever
applies to one.

`max_connections` stays at 100. Raising it is a false comfort: PGPROC memory is trivial but each
backend can allocate `work_mem` per sort or hash node on top of a several-MB baseline, and the `db`
container has a 2 GB limit. The goal is fewer, reused, attributed backends — not more slots.

**pgbouncer is deferred, with triggers**: adopt it in transaction mode when the sum of role limits
exceeds ~80, when k8s web replicas exceed 2, or when connection setup shows up in `db` CPU. Its
prerequisites are already satisfied by this design (role-level GUCs rather than client `options=`),
except `DISABLE_SERVER_SIDE_CURSORS`, which the `.iterator()` callers tolerate.

**psycopg3 is deferred and unblocked.** `django-postgres-extra` is installed but never imported
anywhere in `src/` — it is not the blocker it was assumed to be. Django 6's native pool needs
psycopg 3 and is redundant once thread counts bound the connection population; if pgbouncer is
adopted, skip it entirely.

Zero-risk companion, shipped first: `application_name` per process role, so `pg_stat_activity`
attributes backends per tier before any of the above exists.

## Decision 4: Celery gets queue classes, and Valkey gets split

Three queues — `interactive` (a user is waiting or a safety check-in is due), `bulk` (minutes, has
a progress bar), `maintenance` (beat) — declared on the task decorator per that module's existing
rule, drained by separate containers, with a startup check that fails on a task with no queue.
Without this, a nightly backup and a user's safety check-in reminder contend for the same four
prefork children.

Valkey splits into a broker (`noeviction`, persistent, deliberately capped ~1 GB so a runaway
producer is loud rather than silent) and a cache (`allkeys-lru`, 8 GB of the 10 GB Jess allocated on
2026-09-10, no persistence). Today one 512 MB `volatile-lru` instance holds the broker, the channel
layer, sessions, the Django cache and the map cache.

Two failure modes, and the second is the sharper one. **On fill**, the TTL'd population — the map
cache and the Channels keys — is evicted first, and once only broker keys remain every write fails
with OOM. **On outage**, there is no such thing as losing only the cache:
`CELERY_BROKER_URL = os.getenv("UL_CELERY_BROKER_URL") or VALKEY_URL` (`settings/base.py:374`), and
`UL_CELERY_BROKER_URL` is unset by default, so the instance that holds the cache *is* the broker.
Pausing it takes the session store, the channel layer, the result backend and task enqueueing with
it. There is no configuration in which that coupling is intended; it is what a shared default
produced. Found by the infrastructure repo while implementing the chaos scenarios (N16/N17), which
is also why the "Valkey paused" and "worker paused" scenarios in that ask are not independent tests.

Sizing is generous because the memory exists, but the architecture does not assume it: see D12 on
why the database path must stay correct with the cache empty.

## What this does not decide

Whether `UL_UNTRUSTED_PARSE_POLICY` moves off `warn`. Jess asked for extreme caution about that
specific flag and it is not part of this work.

## How each claim here can be re-checked

- Worker-class heartbeat behaviour: read `gthread.py:409-419`, `sync.py:58-61`, `arbiter.py:604-623`
  in `.venv/lib/python3.12/site-packages/gunicorn/`.
- Connection attribution: `SELECT usename, application_name, state, count(*) FROM pg_stat_activity
  GROUP BY 1,2,3` during a load run.
- The invariant itself: the neighbour test in PL7 — user B's p95 under a fixed arrival rate while
  user A runs each heavy action. It is written to fail against today's build.
