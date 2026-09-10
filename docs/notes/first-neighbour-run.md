# X15 — The first neighbour run, and the correction: on the real process model one user filtering costs another user nothing

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X15` · `status: holds` · `updated: 2026-09-10`

The owner's requirement is *"no action a user takes should impact the availability of the site for
other users, ever"*. This is the first direct measurement of it.

**Read the correction below before quoting anything from the first table.** The run that produced it
used a *development* process model — `runserver` under daphne — and a later run on the real one
(gunicorn + gevent, with the connection cap) moved the single-user result by a factor of twenty. The
headline this document originally carried, that one user pressing a filter button costs another user
4.4 seconds, **does not hold on the process model production runs**.

## Method

`bin/run_perf_tests.sh` against the `development_main` stack on chiron, 2026-09-10 16:08–16:19Z.
`e2e-heavy` seeded to 20,000 pins on one shared label; `e2e-secondary` browsing at a fixed 5
requests a second throughout, rotating `/health/ready`, `/dashboard/map/pins/?limit=50` and
`/dashboard/map/`. The neighbour's latency is the result; the actor's is not.

Budget **699 ms**, derived from a baseline pass taken sixty seconds earlier on the same host
(p95 233 ms), not from a fixed number.

## Results

| phase | what the actor was doing | neighbour count | p50 | **p95** | max | vs baseline |
|---|---|---|---|---|---|---|
| `idle` | nothing | 250 | 69 ms | **228 ms** | 267 ms | — |
| `map_init_1` | 1 user, `map.init` | 250 | 369 ms | **778 ms** | 1.0 s | 3.4x |
| `map_init_4` | 4 users, `map.init` | 233 | 4.8 s | **8.7 s** | 9.9 s | 38x |
| `map_search_1` | **1 user**, one filter POST at a time | 246 | 200 ms | **4.4 s** | 8.3 s | **19x** |
| `map_search_8` | 8 users filtering | 397 | 1.5 s | **8.9 s** | 10.2 s | 39x |
| `label_edit` | 1 user recolouring the shared label | 400 | 148 ms | **340 ms** | 1.9 s | 1.5x |
| `search_storm` | 60 users filtering | 314 | 4.3 s | **60.0 s** | 60.0 s | timeout |

Also recorded: **231 dropped iterations** — 40% of the arrival schedule could not be started at all
because every allocated VU was inside a request when the next was due. **150 of the neighbour's
requests (6.2%) failed outright.** k6 grew the neighbour's pool from 20 VUs to 261 trying to hold 5
requests a second.

`checks{guard:signed_in}` passed 2,503 of 2,503, so the run measured authenticated pages rather than
the sign-in page — which is not a given; see the instrument note below.

**The single-user rows are the ones that matter.** `map_search_1` is one person pressing a filter
button, and it takes the other user's page loads from 228 ms to 4.4 seconds. No concurrency, no
abuse, no automation — one user, one button.

## Why: measured, not inferred

One filter POST on that account, timed directly afterwards on an idle stack:

```
map.search POST 200   11,324,048 bytes   5.26 s
```

**10.8 MB and 5.3 seconds for one button press**, 566 bytes per pin on the wire. `py-spy` on the
saturated process named the frame:

```
    iterencode (json/encoder.py:258)
    dumps (json/__init__.py:238)
    json_script (django/utils/html.py:103)
    render (django/template/backends/django.py:107)
    search_map_post (urbanlens/dashboard/controllers/maps.py:536)
```

The payload dicts, the JSON string and the rendered HTML all exist at once, per request, inside the
request. At the peak the container held **~75 concurrent request threads** and sat at its **2 GiB
memory limit**; 60 concurrent responses are 648 MB of body alone before any of the intermediate
representations. This is what D12 §3.4 proposes to remove by returning data instead of an HTML
document.

The same dump caught P102 on another thread — `Label.save` → `refresh_map_pin_cache_for_label` →
`upsert_pin`, one Valkey round trip per carrying pin, synchronously inside the request. Note though
that `label_edit` is the *best-behaved* phase in the table (p95 340 ms). The fan-out is real and
should still go, but on this evidence it is not what makes the site unavailable — the map payload
is. That is worth saying plainly, because the prior expectation was the opposite.

## Connection pool

`bin/perf/pg_activity_sampler.sh`, 1,030 samples at 1 Hz:

```
peaked at 75/100 backends (75%) at 16:17:21Z
  74  postgres/urbanlens-web
   1  postgres/psql
17% idle at the peak
```

The `application_name` change from phase 2 is what makes that attributable: 74 of 100 connections
were the web tier, and unlike P104's outage they were *working*, not idle. Worth noting the sampler
reported "never close to full" because its pressure threshold is 80% — at 75/100 with 74 from one
tier that reads as too lax, and is probably the wrong threshold.

## Corrected 2026-09-10: the same measurement on the real process model

`dev_env.py create --environment staging` gives gunicorn + gevent with the phase-2 flags
(`--worker-connections 20 --backlog 256 --max-requests 1000`). Same harness, same 20,000-pin account,
same 5 req/s neighbour, host quieter (the development stack stopped, and the environment's own
out-of-path services with it). Budget 563 ms, derived the same way.

| | runserver + daphne | **gunicorn + gevent, capped** |
|---|---|---|
| idle baseline p95 | 241 ms | **183 ms** |
| **`map_search_1`** — one user filtering | **4,431 ms** | **221 ms** |
| `map_search_8` — eight users filtering | 8,902 ms | **1,281 ms** |
| dropped iterations | 231 (39% of the schedule) | **0** |
| failed requests | 19.2% | **0%** |
| peak Postgres backends | 75/100, 74 of them web | **14/100** |
| k6 VU pool | grew 20 → 261 | **29, never grew** |

**One user filtering is now indistinguishable from idle** — 221 ms against a 183 ms baseline. The
19x degradation this document led with was substantially an artifact of the development server's
threading, not the endpoint's cost. That is a large correction to the strongest claim here and it
was found by running the thing rather than reasoning about it.

Three further things the run settles:

- **The `--worker-connections 20` cap works.** 14 backends at peak against 75 before, on identical
  work. This is the first time P104's fix has been exercised, and it is the mechanism it was written
  for.
- **`map_search_8` still misses the budget** at 1,281 ms against 563 ms — about 7x the baseline.
  Concurrent filtering is a real cost that gunicorn absorbs far better than daphne and does not
  remove. The 11.3 MB payload measurement stands, and so does the case for D12's data contract.
- **No worker was killed.** The only lifecycle events were clean `--max-requests 1000` recycles
  ("Worker exiting" / "Booting worker"), no `WORKER TIMEOUT`, no SIGKILL. The gevent heartbeat
  starvation predicted in D11 §2.1 did not appear at this load — which is not the same as it not
  existing; `search_storm`, `map_init` and the import phases have not yet run on this model.

### `search_storm` on the real process model: the cap works, and the invariant fails anyway

Run separately, same environment, budget 567 ms:

| phase | count | p50 | p95 | max |
|---|---|---|---|---|
| `idle` | 250 | 69 ms | 189 ms | 427 ms |
| `search_storm` — 60 users filtering | 148 | **60,000 ms** | 60,001 ms | 60,006 ms |

**The neighbour's median request did not complete.** p50 is the harness's own 60-second timeout, and
**173 dropped iterations — 94.8% of the arrival schedule** could not be started at all. 31% of what
did start failed.

Three things settled at once, and they do not all point the same way:

- **`--worker-connections 20` does exactly what it was written to do.** Peak **61/100 backends, 60 of
  them web, 0% idle** — `20 x 3 workers`, to the connection. Under daphne the same work reached 75
  with nothing bounding it. P104's mechanism cannot recur while this flag is set.
- **D11 §2.1's prediction did not happen.** Zero `WORKER TIMEOUT`, zero SIGKILL, no worker died. The
  reasoning was that a CPU-bound request starves gevent's heartbeat greenlet and the arbiter then
  kills the whole worker at `-t 180`. These requests are database-bound rather than CPU-bound, so the
  heartbeat kept being scheduled. The prediction is not disproved in general — `map_init` and the
  import phases are the CPU-bound ones and have not been run here — but it is not what happens under
  a filter storm.
- **And the neighbour is starved completely.** Not degraded: starved. Every one of the 60 slots is
  held by one account's requests, and the other account's queue behind them in the backlog.

**The cap bounds the blast radius, not the fairness.** It protects Postgres — and therefore every
other tier sharing it — from one user's storm. It does nothing whatsoever to stop that user
occupying the entire request pool. That is precisely the gap D11 §2.2's `app-heavy` bulkhead and the
per-session `limit_conn` exist to close, and this is the first measurement showing the connection cap
alone is not enough.

Worth naming honestly: on this metric gunicorn looks *worse* than daphne, whose storm p50 was 4.3 s
against 60 s here. That is the trade the cap makes. Unbounded threads let every request trickle
through slowly; a bounded pool serves 60 requests properly and queues the rest absolutely. The second
is better for the database and worse for the starved user, and neither satisfies the invariant.

### `map_init` on the real process model: the CPU-bound phase, and the prediction still does not fire

`map.init` serialises the whole account into one HTML document — Python work, not database work.
This is the phase that was supposed to test D11 §2.1. Budget 567 ms:

| phase | count | p50 | **p95** | max | daphne p95 |
|---|---|---|---|---|---|
| `idle` | 250 | 68 ms | **192 ms** | 359 ms | 241 ms |
| `map_init_1` — one user | 250 | 75 ms | **374 ms** | 592 ms | 778 ms |
| `map_init_4` — four users | 250 | 271 ms | **1,065 ms** | 1,535 ms | 8,735 ms |

**`map_init_1` now passes the budget.** One user serialising their whole 20,000-pin account costs
the neighbour 374 ms against a 192 ms baseline and a 567 ms ceiling. `map_init_4` is 8x better than
under daphne and still 5.5x baseline, so four of them is a real cost — but nothing failed, nothing
was dropped, and the VU pool never grew past 25.

Peak backends: **9/100, 89% idle**. This work is CPU, not connections, which is why the connection
cap neither helps nor hurts here.

**D11 §2.1's prediction did not fire again, and the reason is now clear.** The argument was that a
CPU-bound request starves gevent's heartbeat greenlet, so the arbiter concludes the worker is hung
and SIGKILLs it at `-t 180`, taking every co-resident greenlet with it. Zero kills here, under the
most CPU-bound phase in the suite. The mechanism is real but the threshold matters: these requests
cost a second or two of CPU each, and the heartbeat gets scheduled between them. Starving it for 180
consecutive seconds needs a request that *runs* for 180 seconds.

There are exactly two known candidates for that, and both have their own entries: **P108**, where a
20,000-pin map page spends ~7 minutes in a pairwise Haversine scan, and **P96**, where a 20,000-pin
import would run 78 minutes. So §2.1's concern should be read as a property of those two defects
rather than of heavy endpoints generally — which narrows what the gthread move has to buy.

### The whole suite on the real process model

Every phase, measured against gunicorn + gevent with the phase-2 caps, the outbound guard on, and
P110's fix in place. Budgets 563–568 ms, each derived from its own run's baseline.

| phase | daphne p95 | **gunicorn p95** | verdict |
|---|---|---|---|
| `idle` baseline | 241 ms | **190 ms** | — |
| `map_search_1` — one user filtering | 4,431 ms | **221 ms** | ✅ |
| `map_init_1` — one user | 778 ms | **374 ms** | ✅ |
| `label_edit` — the P102 fan-out | 340 ms | **195 ms** | ✅ |
| `import_confirmed` | 11,191 ms | **210 ms** | ✅ |
| `cooldown` — after the import | **60,001 ms** | **206 ms** | ✅ |
| `map_init_4` — four users | 8,735 ms | 1,065 ms | ✗ 5.5x baseline |
| `map_search_8` — eight users | 8,902 ms | 1,281 ms | ✗ 6.7x baseline |
| `search_storm` — sixty users | 60,001 ms | 60,000 ms **p50** | ✗ starved |

Across the `label_edit`/`import_confirmed`/`cooldown` run: **0 dropped iterations, 0 failed requests,
0 worker deaths, VU pool never above 22, and the Celery queue drained to 0**.

**Everything one user can do alone now costs the neighbour nothing measurable.** The two phases that
still fail are concurrency — four or eight users doing the same expensive thing at once — and the
storm, where the failure is total.

**The `cooldown` row is the day's biggest single change: 60 seconds to 206 ms.** That phase existed
to catch damage outliving the action, and under daphne it caught exactly that (P109's task tail, made
of Overture reads that could not be narrowed). Here the import's fan-out drained to zero and the
neighbour never noticed.

**Read that with the caveat it deserves.** This environment runs `UL_ALLOW_OUTBOUND_APIS=false`, so
the enrichment tasks fail fast instead of spending minutes each on the wire. Production makes those
calls for real and *should*. So what is demonstrated is that the tail is not inherent to the import —
it is the provider work behind it — and **P109's shape is unchanged**: one queue, no class of
service, and a safety task still able to queue behind one user's import. The fix for that is PL7
phase 6, and this run does not substitute for it.

P108 and P110 are properties of the code and are unaffected by the process model; P111 was found on
this environment and is a property of the deployment.

## What this cannot tell you

- **The process model is not production's.** A development environment runs `runserver` under
  daphne, which spawns an unbounded thread per request. Gunicorn with `--worker-connections 20`
  across three workers would refuse the 61st connection rather than accept it and swap. The *shape*
  here — one action multiplying another user's latency — is a property of the endpoint's cost and
  is not a dev-server artifact, but the absolute figures are not production's.
- **The load generator shared the host.** k6 reached 261 VUs on the same 8-core box, so the
  high-concurrency rows include the generator's own CPU. `map_init_1` and `map_search_1` are the
  clean rows: one actor VU, ~20 neighbour VUs, and still 3.4x and 19x.
- **The first run did not finish.** It was killed during `import_confirmed` when the host ran short
  of memory. That phase was measured separately on 2026-09-10 with
  `--phases import_confirmed,cooldown`, and it is the worst result in this document:

  | phase | count | p50 | **p95** | max |
  |---|---|---|---|---|
  | `idle` | 250 | 70 ms | **241 ms** | 1.3 s |
  | `import_confirmed` | 1,092 | 93 ms | **11.2 s** | 26.9 s |
  | **`cooldown`** (import over) | 354 | 258 ms | **60.0 s (timeout)** | 60.0 s |

  175 dropped iterations, **19.2% of the neighbour's requests failed**, and `/health/ready` itself
  began timing out. **Cooldown is worse than the acting phase**, which is the finding: the damage
  outlives the action, and the user who caused it received their 504 (P96) and left minutes earlier.

  The cause is P110 — enrichment tasks reading Overture's whole buildings theme because Overture
  rate-limited the STAC index that keeps those reads small. That is what "not decoration" in
  `schedule.js`'s comment on the cooldown phase was written for, and it earned its place first time
  out.

  The import endpoint itself was then measured directly instead, on an idle stack: **500 pins in
  116.6 s into a 20,000-pin account, and 504 at 120.0 s into an empty one** — 233 ms per imported
  pin either way, so the cost is per imported pin rather than per existing one. nginx's
  `proxy_read_timeout 120s` therefore caps a *successful* import at about 510 pins, and the
  empty-account run returned the 504 page to the client and then created all 500 rows anyway. See
  P96, which now carries those numbers.

## Re-running it

```bash
bin/run_perf_tests.sh --url http://localhost:21810 \
    --provision-container urbanlens_development_main_app \
    --db-container urbanlens_development_main_db --heavy-pins 20000
```

Purge first (`provision_integration_env --purge --execute`) if a previous run left the heavy account
grown by an import. Expect ~18 minutes and a heavily loaded box; do not run it beside anything whose
timing matters.
