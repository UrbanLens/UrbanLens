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
| 1 | The neighbour test (k6), per-endpoint pytest gates, chaos specs — all red | not started |
| 2 | Config-only shedding and telemetry, deployable under today's gevent | not started |
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

Two harness corrections go in here: `SeedScalingMixin` gains an `ANALYZE` after seeding (N10 — a
missing `ANALYZE` produced a 12× artifact that cost the previous session three rounds), and
`RenderTimeScalingMixin` switches from `perf_counter` to `process_time`, since CPU time does not
inflate under host contention and is exactly the defect class R27 found.

**Chaos specs.** A Playwright project gated on an environment flag, with injection from the infra
repo (guarded against production and staging container prefixes). Valkey paused, Postgres at its
connection ceiling, a worker stopped, a rebuild lock held.

## Notes for whoever picks this up

- A `dev_env.py` environment writes `UL_ENVIRONMENT=development`, so `init.py` runs Django's
  `runserver` and **no dev env exercises gunicorn at all**. N14 says pytest and local dev never build
  P104's topology; the dev-env tool does not either. Phase 1 depends on a `--environment staging`
  option in the infra repo.
- Both production and staging currently run `-t 600 -k gevent` with no `--max-requests`: the
  timeout work in `f2623a5d4` is committed but not deployed. Nothing on `release/v_0_8_0` is live.
- Do not deploy to staging as part of this work. Use a dev environment; staging is Jess's call.
- `UL_UNTRUSTED_PARSE_POLICY` stays at `warn`. Moving it is explicitly out of scope for this
  programme and needs its own decision.
