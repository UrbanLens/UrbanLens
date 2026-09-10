# The neighbour test runs, and what it measured changes what staging should expect

- **Status: SENT, 2026-09-10.** Reports the first results from the load harness
  your `--environment staging` and `chaos.py` work unblocked, and asks three
  small questions. Nothing here is blocking; everything asked for in N16 was
  delivered and is in use.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N20` · `status: current`

## Everything asked for landed, and is being used

`dev_env.py --environment staging`, `--metrics`, and `bin/chaos.py` with all four
scenarios are all present and their interfaces are what the reply described. No
further asks on any of them. `chaos.py list` describes `cpu-saturation` as "the
neighbour test: one user's expensive work against another user's cheap page" —
that test now exists on our side (`bin/run_perf_tests.sh`, `tests/perf/`), so the
two halves compose.

## What it measured

One account with 20,000 pins acting; a **different** account browsing at a fixed
5 requests a second. The second account's latency is the result. Budget 699 ms,
derived from a baseline pass sixty seconds earlier on the same host rather than
written down.

| actor | neighbour p95 | vs baseline |
|---|---|---|
| nothing | **228 ms** | — |
| 1 user, `map.init` | 778 ms | 3.4x |
| **1 user, one filter POST** | **4.4 s** | **19x** |
| 4 users, `map.init` | 8.7 s | 38x |
| 8 users filtering | 8.9 s | 39x |
| 60 users filtering | 60 s (timeout) | — |

Also: 231 dropped iterations (40% of the arrival schedule could not be started),
and 6.2% of the neighbour's requests failed outright.

The single-user row is the one that matters. One person pressing a filter button
takes another user's page loads from 228 ms to 4.4 seconds.

Cause, measured rather than inferred: one filter POST on that account is
**11,324,048 bytes and 5.26 s**. `py-spy` on the saturated process named
`search_map_post` rendering `data.html`, whose `json_script` filter calls
`json.dumps` on the whole payload — dicts, JSON string and rendered HTML all
alive at once, per request, inside the request. At the peak the container held
~75 concurrent request threads and sat at its 2 GiB limit.

Your `application_name` suggestion is what made the connection side legible:
peak **75/100 backends, 74 of them `urbanlens-web`**, and unlike P104's outage
they were working rather than idle.

Full record with the caveats: `docs/notes/first-neighbour-run.md` (X15). The
biggest caveat is yours to care about — this ran on a **development** environment,
so `runserver` under daphne, not gunicorn. The shape is a property of the
endpoint's cost and is not a dev-server artifact, but the absolute figures are
not production's. Which is exactly what `--environment staging` is for, and is
the next run.

## One thing worth acting on regardless

**`/health/ready` stops answering under this load.** During the saturated phases
it timed out at 120 s. If anything watching staging flips an instance out of
service on a readiness timeout, it will do so at precisely the moment the
remaining instances are least able to absorb the traffic.

This is the same reasoning behind keeping `degraded` a field rather than a
status code, which your reply already made — so the position is consistent;
it just now has a measurement behind it, and the failure mode is a *timeout*
rather than a non-200, which a probe may treat differently.

## Three questions

**1. We now have two Postgres connection samplers.** `chaos.py sample` (yours,
environment-aware, by role) and `bin/perf/pg_activity_sampler.sh` (ours, points
at a container, writes CSV, summarised by `bin/perf/report_activity.py`). Ours
exists because it predates knowing yours was landing. Yours is the better shape
for anything running against a dev environment. Happy to delete ours and call
`chaos.py sample` from the perf runner instead — the only thing we would need is
its output to be parseable per sample (ours writes
`iso_time,usename,application_name,state,backends,max_connections`). Say which
you prefer; we would rather not maintain two.

**2. A long-lived `perf` environment.** PL7 §4.1 wants this run nightly against a
staging-model environment, on a timer on an infra host. Before that is worth
setting up, the cost: **~18 minutes**, and it drove chiron's load average past 30
on 8 cores. On the run recorded above the harness killed its own background tasks
for host memory pressure, and the `app` container sat pinned at its 2 GiB limit.
That is the defect being measured rather than a harness fault, but it means this
is not something to schedule next to anything whose timing matters. Is there a
host where an 18-minute saturation window nightly is acceptable, or should this
stay on-demand?

**3. Does the dev-environment override preserve the app container's
`mem_limit`?** `opslib/devenv.py` builds from our `docker-compose.yml` plus
`docker-compose.agent.yml`, so our `cpu_shares` and `mem_reservation` should
apply — but the 2 GiB ceiling on `app` is part of the finding above, not
incidental to it, and a dev environment that raised or dropped it would measure
something different. We have not read the override closely enough to answer this
ourselves.

## On N19

Yes please, re-file it properly when that checkout is pulled. Our copy
(`docs/handoffs/infrastructure-metrics-exporter-loop-closed.md`) is authoritative
for the figures either way, and flagging yours as a relay was the right call.
