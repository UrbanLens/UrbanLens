# X28 — 1,000 concurrent users needs twice this app tier's CPU, and the database is no longer the wall

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Status: `holds` · Measured 2026-09-21 against the `ul_perf_*` environment at `615041aa1` + the
audit's fixes. One ladder: 100 → 250 → 500 → 1,000 concurrent users, 240 s at each level, 1,270 s
total, 1,000 seeded accounts, 471,756 pins, 24 tiles a viewport, a pre-seeded 32×32 tile grid.
Run `capacity-ladder3-20260921T160557Z`; `tests/perf/results/` is gitignored, so it is on `chiron`
only.

Container limits: `ul_perf_app` 4 cores / 2 GB, `WEB_CONCURRENCY=6`, gthread, `--threads 4`
(24 request threads); `ul_perf_db` 4 cores / 4 GB with the compose tuning from `c46b0c9f5`.

**Not comparable** with anything before `615041aa1` (the harness's browser-cache model changed and
stopped suppressing 73% of its own tile load), before `f346409c5` (the seeder wrote where the proxy
had stopped reading, so those runs measured a cold cache), or with `capacity-ladder2` of the same
day, whose WebSocket handshakes failed 100% against a stale container.

## What holds

| hold | users | requests failed | ws handshakes ok | app mean cores | app throttled | db mean cores | db throttled | proxy p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| u100 | 100 | 0.00% | 100% | 0.37 | 0.00% | 0.26 | 0.00% | 0.183 s |
| u250 | 250 | 0.00% | 100% | 1.04 | 0.54% | 0.55 | 0.00% | 0.198 s |
| u500 | 500 | 0.00% | 100% | 2.01 | 2.92% | 1.13 | 0.08% | 0.231 s |
| u1000 | 1000 | 0.00% | 100% | 3.68 | 32.89% | 1.75 | 0.16% | 6.218 s |

**Nothing failed at any level.** No 5xx in the proxy log, no failed request in k6, every WebSocket
handshake accepted. Over-capacity here is latency, not errors, which is the failure mode to want.

**500 concurrent users pass.** One endpoint is over its budget — `search_panel` at p95 554 ms
against 500 ms, which is P132 and nothing to do with capacity. Pages sit at p95 121–505 ms against
a 1,000 ms budget, fragments at 43–449 ms against 500 ms, and a basemap tile at 46 ms against 300.

**1,000 concurrent users do not.** Every endpoint is over: pages p95 5.5–8.6 s, tiles 6.1 s. The
cause is one number — the app container wanted 4.02 cores and had 4.

## Why 1,000 is the knee

App CPU is linear in users up to the limit and then stops being linear:

| users | app mean cores | cores per 100 users |
|---:|---:|---:|
| 100 | 0.37 | 0.37 |
| 250 | 1.04 | 0.42 |
| 500 | 2.01 | 0.40 |
| 1000 | 3.68 (clipped) | 0.37 (clipped) |

At 0.40 cores per 100 users, 1,000 users demand **≈ 4.0 cores** against a 4-core limit, which is
why the container throttles 32.89% of its periods and p95 goes from 231 ms to 6.2 s across one
doubling. The 3.68 figure is not demand; it is the ceiling with the bursts shaved off.

**To serve 1,000 users at the latency 500 users get, the app tier needs 8 cores.** That is the same
50% utilisation, not a safety-factor guess: at u500 the app ran at 2.01 of 4.

Memory scales with workers, not with users: 1,731–1,743 MB peak at every level from 100 users up,
on 6 workers — about 290 MB a worker, flat. Twelve workers need roughly 3.5 GB, so an 8-core app
tier wants `MEM_LIMIT__APP=4g`, and the current `3g` in `production.sample.env` would not hold it.

## The database is no longer the wall

P125 concluded that 500 users failed on Postgres' own 2-core limit. With `CPU_LIMIT__DB=4` and the
tuning `c46b0c9f5` added, Postgres at **1,000** users is at 1.75 mean / 3.39 peak cores and
throttles 0.16% of its periods. It is not what 1,000 users are waiting for.

It is close to being what the *next* configuration waits for. Database CPU is linear in users the
same way app CPU is, at **0.226 cores per 100** (0.26 at u100, 0.55 at u250, 1.13 at u500), so
1,000 users actually served — rather than queued behind a throttled app tier — want about **2.3
mean database cores of 4**. That is comfortable on the mean and not on the bursts: peak/mean is
1.9 at u500 (2.14 against 1.13), which puts the peak at 1,000 users near 4.3 cores, over the limit.
`CPU_LIMIT__DB=6` is headroom for that, not a second wall being cleared. The 1.75 mean measured at
u1000 is below the 2.26 this predicts precisely because the app tier was throttled and could not
drive the database that hard.

## Where the CPU goes

From `request_costs.txt` (whole run, all four levels):

| view | count | CPU share | mean CPU ms | mean SQL ms | mean queries | max queries |
|---|---:|---:|---:|---:|---:|---:|
| `map.view` | 4,095 | 16.0% | 63.8 | 81.6 | 18.0 | 20 |
| `pin.details` | 1,784 | 10.5% | 95.6 | 156.5 | 29.3 | 45 |
| `search.panel` | 1,366 | 7.3% | 86.8 | 238.1 | 34.0 | 67 |
| `map.basemap_tiles` | 46,314 | 6.0% | 2.1 | 0.1 | 0.0 | 3 |
| `map.autocomplete.local` | 3,135 | 5.9% | 30.6 | 266.2 | 8.8 | 11 |
| `organize.index` | 664 | 5.8% | 141.8 | 234.3 | 21.0 | 23 |
| `map.search` | 5,082 | 5.5% | 17.7 | 28.3 | 5.0 | 10 |
| `messages.conversation` | 705 | 5.4% | 124.5 | 221.6 | 40.0 | 42 |
| `home.view` | 1,084 | 5.2% | 77.6 | 197.4 | 34.0 | 36 |

**The basemap tile work is done.** Tiles are 46,314 of the run's 82,852 application requests —
**55.9%** of everything the deployment serves — and **6.0% of its CPU**, at 2.1 ms and zero queries
each. X26 measured the same share of requests when they were the most expensive thing on the
site. The share
of traffic a vector basemap would remove has not changed; the cost it would remove has almost gone.

**That 2.1 ms is a cache hit, and this harness only ever hits.** The grid is 1,024 seeded tiles and
a viewport asks for 24 of them, so every tile in this run was warm. A real population spread over a
continent misses, and a miss is an upstream fetch of 0.43–0.98 s (P131) held under a concurrency
bound. What this establishes is that a *served* tile costs nothing worth counting — not that tile
traffic is free for a population that has not already drawn the tiles.

**What is left is ordinary query cost on the pages.** `map.autocomplete.local` spends 266 of its
311 ms in SQL for 8.8 queries — that is P100, the nine leading-wildcard `ILIKE`s with no trigram
index, and it is now the worst SQL-per-call on the site. `search.panel` is 238 ms of SQL over 34
queries returning 1,268 rows (P132). `messages.conversation` and `home.view` are 40 and 34 queries.
Nothing here is a cache problem any more; it is query counts and query plans.

## What to do with this

1. **`CPU_LIMIT__APP=8`, `WEB_CONCURRENCY=12`, `MEM_LIMIT__APP=4g`, `CPU_LIMIT__DB=6`** is the
   configuration this measurement points at for 1,000 users. Nothing has measured it; the 8 is an
   extrapolation from a linear region, and the memory figure assumes per-worker cost stays flat.
2. **P100 and P132 are the next real work** — they are the two biggest SQL costs per call, and
   neither is fixed by more CPU.
3. **None of this describes damballa.** Production still runs `-k gevent`, `WEB_CONCURRENCY=3` and
   no cgroup limits at all (N26, #23). Every figure here is the `ul_perf_*` stack.

## Related

P100, P125, P131, P132, X26, X27, N26.
