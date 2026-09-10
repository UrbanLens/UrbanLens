# X15 — The first neighbour run: one user's ordinary actions multiply another user's latency by up to 39x

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X15` · `status: holds` · `updated: 2026-09-10`

The owner's requirement is *"no action a user takes should impact the availability of the site for
other users, ever"*. This is the first direct measurement of it. It is violated today, by a wide
margin, by a single user performing one ordinary action.

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
