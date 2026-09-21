# X26 — The capacity run after REData's vector switch: tiles are still half the deployment's traffic, and the rest of the delta is a confound

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Status: `holds` · Measured 2026-09-20 against the `ul_perf_*` environment, one 500-user hold of
240 s, 1,000 accounts. Runs: `capacity-20260920T042859Z` (before) and
`capacity-20260920T183609Z` (after). `tests/perf/results/` is gitignored, so those directories are
on `chiron` only.

## What holds

**Basemap tiles are still about half of everything the deployment serves.**

| run | total requests | `basemap_tile` | share | tile p95 | budget |
|---|---:|---:|---:|---:|---|
| 04:28Z | 16,421 | 8,646 | 52.7% | 1144 ms | over (300 ms) |
| 18:36Z | 16,438 | 8,524 | 51.9% | 477 ms | over (300 ms) |

That share is the number worth carrying forward, and only as a share of *requests*. It is what a
vector basemap removes from this deployment entirely — a MapLibre client fetches one style document
and then reads the PMTiles archive directly, so none of those 8,524 requests reach us. It is also
the reason the tile path got its own proxy, cache and concurrency bound in the first place.

**It stopped being a share of cost on 2026-09-21.** X28 re-measured the same endpoint after the
tile work landed: still 58% of all requests, and **6.0% of app CPU**, at 2.1 ms and zero queries
each. A vector basemap still removes 58% of the requests this deployment answers; it no longer
removes a comparable share of what answering them costs, and it is not what stands between this
deployment and 1,000 concurrent users.

## What does not hold: the across-the-board improvement is not ours

Every endpoint in the run improved, between 10% and 78%, including ones nothing in this work
touches (`notifications_view` −78%, `conversation` −68%, `map_search` −73%). A uniform shift across
unrelated endpoints is a property of the host, not of a change to any of them.

The mechanism is visible in the container samples:

| run | `ul_perf_db` throttled | `ul_perf_app` throttled |
|---|---:|---:|
| 04:28Z | 32.21% | 3.25% |
| 18:36Z | 16.48% | 5.02% |

Database CPU throttling halved between the two runs. `provision_integration_env` also reports
`analyzed=True`, so the second run began with fresh planner statistics. Either is enough to move
every query-bound endpoint at once.

**So: do not read these two runs as a before/after of anything.** The tile share is comparable
because it is a count, not a latency. The latencies are not.

## Two traps this run fell into, both worth knowing

**`--manifest` skips tile seeding.** `run_capacity_tests.sh` seeds the tile cache only inside the
`--provision-container` branch. A run given a ready-made manifest seeds nothing, so every tile is
an upstream fetch, hits the proxy's slot bound, and answers 503. The first attempt at this
measurement did exactly that: 8,360 of 8,352 tile requests were 503s, and the report still rendered
a full table of plausible-looking latencies.

**The pre-flight check only tests one tile.** `population.js` fails the run if the *first* tile of
the grid is not 200. In the failed attempt that one coordinate had been hand-seeded earlier while
testing, so the guard passed and the other 1,020 coordinates were cold. A guard that samples one of
1,024 cells does not establish the cache is warm; the tell was the proxy error count, not the guard.

Seed explicitly when passing `--manifest`:

```sh
docker exec <app> /app/.venv/bin/python src/urbanlens/manage.py \
    seed_basemap_tile_cache --layer terrain --zoom 13 \
    --origin-x 2400 --origin-y 3072 --size 32 --catalogue
```

The layer comes from `TILE_LAYER` in `tests/perf/k6/lib/capacity.js`, which is `terrain` rather than
`street` since REData published street as vector — `seed_basemap_tile_cache` writes placeholder
bytes straight into the cache and never contacts REData, so for a seeded run the layer is only a
cache key, but nothing can fill a street key from upstream any more.
