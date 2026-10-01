# X19 — Bigint keys are not what makes fetching a uuid cost a join; the join is, and today's bigint-plus-random-uuid design inserts slowest

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X19` · `type: experiment` · `status: holds` · `updated: 2026-09-15` · `source: scratch report + raw outputs, session of 2026-09-15 (not preserved — scratchpad files vanish at session end; this file is the durable record)`

Investigated whether UrbanLens should move dashboard models from `BigAutoField` primary keys to
UUID primary keys, and, separately, what actually removes the joins the app does today to read a
related row's `uuid`. Two environments:

- **Perf database:** `ul_perf_db`, PostgreSQL 17.5, 2 CPU / 2 GB, the 1,000-account capacity
  population (real code paths, `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`, median of 9 runs).
- **Throwaway benchmark container:** `postgis/postgis:17-3.5` (`ul_uuid_bench_db`), `--cpus=2
  --memory=2g`, destroyed after the run; a second pass throttled the same container to 512 MB with
  `docker update` to see behavior once the working set exceeds cache. Synthetic schema reproducing
  the app's FK/M2M shapes (1 M `loc`, 50 k `label`, 3 M `pin`, 6 M `pin_label`).

## The answer in brief

- **Moving primary keys to UUIDs is not supported by this measurement.** v4 keys cost insert
  throughput, WAL, and random-lookup latency once memory is short; v7 keys cost less but stamp
  every row with its creation time to the millisecond, which is a privacy regression for this app
  if anything ends up client-facing. Neither makes reads faster than bigint on the same query
  shape (within ±15% warm).
- **What removes the join is emitting the FK column instead of joining for the related row's
  key.** That is independent of key type — today's bigint FKs already have this property, the code
  mostly just doesn't use it for the three `uuid` lookups that exist.
- **Today's actual design — bigint PK plus a random (v4) `uuid` unique index on top — is the
  slowest of the four shapes measured for writes**, because every insert maintains both indexes.

## Key figures

**Join vs. FK-column read, perf database** (`EXPLAIN ANALYZE`, median of 9):

| Shape | Heavy account, 20,301 pins | Typical account, 391 pins |
|---|---:|---:|
| pins → `location_id` (FK column, no join) | 6.37 ms | 0.13 ms |
| pins → `location.uuid` (join) | 99.29 ms | 0.70 ms |

Only three ORM call sites do this join for a related uuid, all one gate: `models/comments/queryset.py:49`,
`models/trips/queryset.py:301`, `services/notifications/mentions.py:147` — the comment/trip/mention
location-mention visibility check. See "Correction" below before treating 99 ms → 6 ms as a plain
schema fix.

**Insert throughput, `pgbench -c 4 -j 2 -T 30`, throwaway container, tps (Δ vs. plain bigint):**

| Workload | bigint | bigint + v4 `uuid` (today's design) | v4 PK | v7 PK |
|---|---:|---:|---:|---:|
| Pin insert, 2 GB | 9,302 | 6,564 (−29%) | 7,148 (−23%) | 6,221 (−33%) |
| Pin insert, 512 MB | 9,030 | 4,567 (−49%) | 4,660 (−48%) | 5,752 (−36%) |

Today's design (bigint PK + random-v4 `uuid` unique index) loses as much or more insert throughput
as a full v4 PK conversion would, because every insert maintains both a bigint PK and a random
unique index, at 1.9–2.5× the WAL of plain bigint.

**Random PK-probe latency, 100,000 random keys, throwaway container, ms (median of 5):**

| Scenario | bigint | today's design, by uuid index | v4 PK | v7 PK |
|---|---:|---:|---:|---:|
| 2 GB | 371 | 573 (+54%) | 609 (+64%) | 379 |
| 512 MB | 423 | 4,591 (11×) | 4,763 (11×) | 455 |

**Size, perf database:** 42 indexes on a `uuid` column, 56 MB total. `dashboard_locations` carries
two indexes on the same column: the unique constraint's index and a separate plain index,
`idxdb_loc_uuid` (`src/urbanlens/dashboard/models/location/model.py:407`) — confirmed in the
current working tree, 16 MB each on perf, and every location insert pays for both.

Not re-measured this session: everything above is transcribed from the scratch report's own
measurements taken earlier the same day; this record did not re-run the benchmark or the
`EXPLAIN` queries.

## Correction: the join-removal fix cannot be a plain FK

The scratch report's option C recommends giving `CommentLocationMention` a `location` foreign key
so the comment/trip/mention gate compares `location_id` instead of joining for `location__uuid`,
citing the 99 ms → 6 ms figure above. **That cannot be the shape of the fix as stated.**
`src/urbanlens/dashboard/models/comments/location_mention.py:17-20` documents why the model stores
`location_uuid` rather than an FK:

> ``location_uuid``, not a ``Location`` foreign key, on purpose: a comment naming a uuid that
> resolves to nothing is hidden from everybody (nobody has pinned a location that does not exist),
> and an FK could not record one, which would make such a comment visible to all.

A plain FK cannot hold a dangling reference, so a comment naming a uuid with no matching location
would either fail to save or silently drop the mention row — either way the comment would no
longer be hidden, which is the opposite of the intended behavior. The 99 ms figure is real and is
the size of the prize, but the security property (an unresolvable location-mention must still
suppress the comment) constrains what shape the fix can take. An option consistent with both: a
nullable `location` FK populated only when the stored `location_uuid` resolves to a real row, with
the gate still treating "uuid present but FK null" as unresolved/hidden rather than as absent. Not
implemented or measured this session — recorded as an open option, not a decision.

## Other facts checked against the working tree

- **Duplicate location-uuid index is real**, not a measurement artifact:
  `src/urbanlens/dashboard/models/location/model.py:407` declares
  `Index(fields=["uuid"], name="idxdb_loc_uuid")` alongside the column's own unique-constraint
  index.
- **The comment-mention gate never ran under load when measured.** The perf database held 0
  comments and 0 location mentions at measurement time. The 99 ms is the executed cost of the
  gate's pins → `location.uuid` subquery run on its own; the gate as a whole, over a populated
  comment table, was never executed. Commit `045101506` (this branch) adds
  comments to the capacity population, so the next perf provision run should be able to exercise
  this gate for real.

## Conclusions (not decisions — open for reassessment)

- Keep bigint primary keys; the measurement does not support a uuid-PK conversion at either v4 or
  v7.
- Where a query needs only a related row's key, read the FK column rather than joining for a
  `uuid`; this is independent of key type and available today.
- Drop the duplicate `idxdb_loc_uuid` index (`location/model.py:407`).
- If ids need to be opaque to clients, encode the bigint at the edge with a keyed, type-tagged AES
  token (~1 µs per id in this session's Python 3.12 measurement) rather than storing a second key;
  start with surfaces that are cross-user or otherwise enumerable rather than owner-only screens.
- Retire `uuid` columns once nothing external still references them as a stored identifier.

## Out-of-scope observations

- **Pin-label query on the heavy perf account (20,301 pins) parallel-seq-scans all ~1.4 M
  through-table rows: 229 ms.** Disabling hash joins turns the same query into 20,301 index-only
  probes at 123 ms. Not chased further this session — a planner-choice question, separate from the
  key-type question this record is about.
- **The comment-visibility gate's plan sequentially scans all 398,153 `Location` rows** for the
  viewer's pinned uuids (a hashed SubPlan). Since the perf database held 0 comments, this plan was
  never exercised under real row counts; worth re-checking once `045101506`'s comment population
  is in place.
