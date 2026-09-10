# PL7 — Making "no user can affect another user's availability" a property the tests can prove

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL7` · `status: live` · `updated: 2026-09-10`

Executes D11 (process isolation and the connection budget) and D12 (the map data contract). R27
recorded the map-payload fix that preceded both. Full plan text, including the parts not summarised
here, was written to the session's plan file; this is the tracker.

The requirement: *"No action a user takes should impact the availability of the site for other
users, ever."* Today nothing states that as a test, which is why the 504s and the 11-hour outage
(P104) were both found by users rather than by CI.

## Ordering principle

Tests that state the invariant come **before** the architecture that satisfies it, and are left red
on purpose until it lands. Each phase's acceptance is a measurement, not a review.

| Phase | What | Status |
|---|---|---|
| 0 | Verify HEAD; record P105, P106, N15, D11, D12, PL7; archive P99 | **done 2026-09-10** |
| 1 | The neighbour test (k6), per-endpoint pytest gates, chaos specs — all red | **partly done 2026-09-10** |
| 2 | Config-only shedding and telemetry, deployable under today's gevent | **partly done 2026-09-10** |
| 3 | `gthread`, per-role Postgres users, `app-heavy` pool (D11) | not started |
| 4 | Valkey split + degradable session path (P105) | not started |
| 5 | Map data contract v11 (D12) | not started |
| 6 | Celery queue classes; move P96/P98/P102/P2 work off the request | not started |
| 7 | Observability completion, profiling harness, k8s parity | not started |

## Phase 0 — done 2026-09-10

Ran the five map test files at HEAD (`c42c597d2`) in the app container: **14 passed in 225s**. This
was the one unverified claim left over from R27's session — the last recorded output for
`test_map_payload_instantiation_scaling.py` predated the fix and showed 70 model objects for 10 pins.
It does not any more.

```bash
APP=urbanlens_$(grep ^UL_CONTAINER_NAME .env | cut -d= -f2)_app
docker exec -e UL_TEST_DB_NAME=<unique> "$APP" /app/.venv/bin/python -m pytest -q \
  src/urbanlens/dashboard/tests/hypothesis/test_map_payload_instantiation_scaling.py \
  src/urbanlens/dashboard/tests/hypothesis/test_map_payload_agreement.py \
  src/urbanlens/dashboard/tests/hypothesis/test_map_endpoint_payload_agreement.py \
  src/urbanlens/dashboard/tests/hypothesis/test_map_pin_cache_read_path.py \
  src/urbanlens/dashboard/tests/hypothesis/test_bulk_photo_delete_scaling.py
```

## Phase 1 — partly done 2026-09-10

Landed (`57b234277`, `c03618f6b`), all verified in the app container:

- **The N10 fix.** `SeedScalingMixin.seed()` refreshes planner statistics for
  exactly the tables the seed's own SQL wrote to. Never a bare `ANALYZE`:
  measured 3.55s cold / 1.70s warm across 237 tables against 45ms for three
  named ones, twice per assertion. `test_seed_scaling_analyze.py` proves
  `pg_class.reltuples` actually moves.
- **Ten reproductions**, all `xfail(strict=True)` so the suite is green now and
  turns red the day each fix lands: P102's label fan-out (3), P96's uncapped
  import (4), and the unbounded map document (3). Each group is paired with
  non-xfail guards asserting the seed and the response shape are real.
- **X14**: the proposed `perf_counter` → `process_time` switch for
  `RenderTimeScalingMixin` was measured and **rejected** — CPU separates the
  classes worse (41.2x against 48.5x) and leaves less margin before a false
  failure. The mixin keeps its calibrated clock.
- **`EndpointScalingMixin`** with the bytes/row, rows-fetched/row and
  payload-ceiling axes, plus six known-cost views to prove each can fail. It
  found **P107** on its first real use: `SavedFilterMatchCountsView` reads every
  root pin's uuid to draw one number per saved filter, which all three older
  instruments correctly call flat. `map.pins.children` is gated too — it was
  never moved to the projection path when the main map was.

Two traps found while writing those, both now documented in the test files
because the next person will hit them the same way: a reproduction whose work
happens in `transaction.on_commit` does nothing under a `TestCase`, and
`MapPinCache` declines to act at all without an injected client — so a
fan-out test written the obvious way passes against the broken code. The import
tests were vacuous in their first draft for a third reason (wrong payload keys),
caught by probing the live endpoint rather than by review.

Still to do in this phase: the `EndpointScalingCase` generalisation with the
bytes/row and rows-fetched/row axes (self-contained, here), the k6 neighbour
scenario and its seed command, and the chaos scenarios. The last two block on the
infra-repo asks in N16.

## Phase 2 — partly done 2026-09-10

Four config changes, all deployable under today's gevent and none of them
requiring D11's process-model move first:

- **`--worker-connections 20 --backlog 256`** (`package.json`, reasoning in
  `gunicorn.conf.py`'s docstring since a JSON file cannot carry one). gevent
  defaults to 1000 greenlets per worker and each can hold its own backend under
  `CONN_MAX_AGE=0`, so three workers could demand 3,000 connections against 100.
  Arithmetic: ~32 of the 97 usable slots belong to daphne, the four Celery
  workers and beat, leaving ~65 for the web tier, and 3 × 20 = 60 fits.
  **It bounds the steady state, not the peak** — `timeout_utils.py` holds a
  module-level `ThreadPoolExecutor(max_workers=64)` per worker process whose
  threads can touch the ORM, and nothing caps its connection use. Per-role
  `CONNECTION LIMIT`s (phase 3) are what actually fence that, because a role
  limit makes the web tier fail its own connections rather than starving Celery.
- **`profiles: ["metrics"]` on `celery-metrics`** — N15's fix. The precondition
  was stated in a comment and implemented nowhere.
- **nginx timing fields** in `log_format main`: `rt`, `urt`, `ust`, `uct`, `up`.
  A 504 nginx generated while the worker was still computing was previously
  indistinguishable in the log from one the application returned; `ust=-` versus
  `ust=504` is that distinction.
- **`application_name` per process role** in `DATABASES.OPTIONS`. When P104's
  outage showed 97 of 100 connections idle, nothing recorded which tier held
  them.

Guarded by `test_connection_budget_wiring.py`, which computes the connection
budget from the compose file rather than hardcoding it — so raising
`WEB_CONCURRENCY` or the per-worker cap fails there instead of in production.

- **`RequestTelemetryMiddleware`** and `UL_SLOW_REQUEST_MS` (default 1000). Logs
  wall, CPU and SQL time/statements/rows for any request over the threshold.
  The CPU split is the point: R27's diagnosis turned on 88% Python against 6%
  Postgres, which is what ruled out the index work everyone assumed was needed,
  and a line carrying only a total would not have helped. `sql_rows` is what
  makes P107's shape visible in a log.

  Two corrections came out of writing it. A slow request that *raises* was
  reported nowhere, because the log sat after the `try` — it is in the `finally`
  now. And the belief that motivated that fix was itself wrong: Django wraps
  every middleware in `convert_exception_to_response`, so a view's exception
  arrives here as a 500 and the `raised` branch is rare rather than the common
  case. Both the comment and the test say so now.

- **`/health/ready` connection headroom.** Reports `connections {used, max}`
  and `degraded`, and **stays 200 when degraded**. That is the load-bearing
  choice: a readiness probe flipping to 503 during a connection storm removes
  the instances still able to serve, converting a degradation into an outage —
  which this deployment has done before (the ws manifest's comments record it).
  Alert on the field. The 0.8 threshold is chosen because P104 went from healthy
  to 97-of-100 with nothing in between reporting the climb. Unreadable
  `pg_stat_activity` reports None, meaning *not measured*, never degraded: a
  probe that fails closed on a missing read privilege takes the site down over a
  permissions choice.

Not done in this phase: `pg_stat_statements` on the `db` service,
`cpu_shares`/`mem_reservation`, and the nginx `limit_req`/`limit_conn` zones.

## Phase 1 — the tests that state the invariant

**The neighbour test.** k6, because the assertion is user B's latency *under a fixed arrival rate*
while user A acts. That is an open-model load; a closed-model tool (Locust's default) lets a slowing
server reduce B's request rate, which hides exactly the degradation being measured. B runs 5 req/s
over a real authenticated page, the hot JSON path and `/health/ready`, tagged per phase. A runs one
heavy action per phase: map document ×1 and ×4, filter POST ×1 and ×8, a label edit on a label
carried by 20k pins, a 20k-pin import, a bulk photo delete, and finally 60 concurrent filter POSTs.

Thresholds are derived from a baseline pass, not chosen: `B = min(1000, max(3·p95_base,
p95_base + 250))` ms. A sidecar samples `pg_stat_activity` at 1 Hz and greps the database log for
`53300`; both are hard failures.

Expected red on today's build: the 60-concurrent phase (nothing caps greenlets — P104's mechanism),
the label edit (P102), the import (P96). The map phases are expected green and are regression guards.
Record the first run as an X entry before any fix.

**Per-endpoint gates.** A new `EndpointScalingCase` composes the three existing mixins and adds two
axes they are blind to: bytes per row, and *rows fetched* per row (via `execute_wrapper` summing
`cursor.rowcount`) — the latter is what catches a view that pulls every uuid into Python while its
query count stays flat. Plus a document-cap axis: seed cap+1, assert the response carries at most
cap rows and a continuation marker.

One harness correction goes in here: `SeedScalingMixin` gains an `ANALYZE` after seeding (N10 — a
missing `ANALYZE` produced a 12× artifact that cost the previous session three rounds). **Done.**

`RenderTimeScalingMixin` keeps `perf_counter`. Switching it to `process_time` was this programme's
design recommendation and did not survive being measured — X14 has the numbers: CPU separates the
benign and pathological classes *worse* (41.2x against 48.5x) and leaves a third of the margin
before a loaded host produces a false failure. Neither clock is quiet at these batch sizes; the
instrument that answers N11's concern precisely is `InstantiationScalingMixin`'s object count, which
does not move with load at all.

**Chaos scenarios live in the `infrastructure` repo**, not in this one's Playwright suite — Jess's
call, 2026-09-10, and the right one: the injections are destructive against a host that also runs
production, and that repo's `drills/guard.sh` already encodes the "never touch these stacks" rule
that makes them safe. Four scenarios (Valkey paused, Postgres at its connection ceiling, a worker
stopped, CPU starvation in the app container), specified in full in
[`../handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md`](../handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md).

That handoff also raises the one definitional problem worth settling before anyone writes them:
`drills/README.md` says a drill rehearses a *runbook* and "is not a test suite", and these assert an
application invariant instead of recovering anything — so whether they belong in `drills/` or a
sibling directory is the infra team's decision, not ours.

## Notes for whoever picks this up

- A `dev_env.py` environment writes `UL_ENVIRONMENT=development` (`bin/opslib/devenv.py:963`), so
  `init.py` runs Django's `runserver` and **no dev env exercises gunicorn at all**. That default is
  deliberate — it is what gives hot reload without a bind mount — so the ask is an opt-in flag, not a
  change. N14 says pytest and local dev never build P104's topology; the dev-env tool does not
  either. Phase 1's k6 and chaos halves both block on it. Asked for in
  [`../handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md`](../handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md).
- Both production and staging currently run `-t 600 -k gevent` with no `--max-requests`: the
  timeout work in `f2623a5d4` is committed but not deployed. Nothing on `release/v_0_8_0` is live.
- Do not deploy to staging as part of this work. Use a dev environment; staging is Jess's call.
- `UL_UNTRUSTED_PARSE_POLICY` stays at `warn`. Moving it is explicitly out of scope for this
  programme and needs its own decision.
