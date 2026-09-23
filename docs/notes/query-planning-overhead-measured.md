# X27 — Planning, not execution, is most of this database's time: 79% of it for the short SELECTs that are 92% of all calls

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X27` · `type: experiment` · `status: holds` · `updated: 2026-09-21` · `source: pg_stat_statements and pgbench inside ul_perf_db (postgis/postgis:17-3.5 + pgvector), counters accumulated across the capacity runs of 2026-09-15 to 2026-09-20; session of 2026-09-21`

Every capacity record so far ([X26](capacity-after-vector-basemap-measured.md), [P132](../PROBLEMS.md),
[P125](../PROBLEMS.md)) has priced database work as one number — SQL wall time per request — and
looked for the query to make faster. This record splits that number in two and finds that most of
it is the planner, not the executor. The consequence is that the largest available database win is
not a query change at all.

## Method

The counters come from `pg_stat_statements` on `ul_perf_db`, which has `track_planning = on`. They
accumulated over the capacity runs between 2026-09-15 and 2026-09-20 and were never reset, so they
describe the mix of a k6 capacity ladder (`tests/perf/k6/population.js`) rather than a single run.

```bash
docker exec ul_perf_db psql -U postgres -d urbanlens_perf -X -q -c "
SELECT sum(calls), round(sum(total_plan_time)::numeric,1) AS plan_ms, round(sum(total_exec_time)::numeric,1) AS exec_ms
FROM pg_stat_statements;"
```

pgbench replays the three most-called statements (the `auth_user`, `dashboard_profiles` and
`dashboard_site_settings` single-row lookups — 840,475 calls between them) against the same
database, under each of libpq's three protocols, `-c 8 -j 4 -T 15`, with
`PGOPTIONS=-c pg_stat_statements.track=none` so the instrument is not measuring itself.

## Results — where the database's time actually goes

| slice | calls | planning | execution | planning share |
|---|---|---|---|---|
| everything | 4,534,988 | 3,588,522 ms | 7,626,918 ms | **32.0%** |
| `SELECT`s whose mean execution is under 1 ms | 4,155,677 | 0.271 ms/call | 0.072 ms/call | **79.0%** |

The second row is the web tier: 91.6% of every call the deployment makes. Each of those statements
is planned for roughly four times as long as it runs.

## Results — what preparing them buys

| protocol | tps | mean latency |
|---|---|---|
| `-M simple` | 2,000.5 | 3.999 ms |
| `-M extended` | 1,601.4 | 4.996 ms |
| `-M prepared` | 5,812.3 | 1.376 ms |

**2.9x the throughput of the simple protocol, 3.6x the extended one.** With
`pg_stat_statements.track` left at its normal `all` the same run measures 1,900.8 / 6,229.2 tps, so
the gap is not an artefact of tracking.

The middle row is the trap. psycopg3 will only prepare a statement whose parameters were bound
server-side, and Django overrides psycopg's own `prepare_threshold` with `None` — preparation off —
so that a transaction-pooling proxy in front of Postgres keeps working
(`django/db/backends/postgresql/base.py`, `get_connection_params`). There is no such proxy here.
Set `server_side_binding` without `prepare_threshold` and every query pays an extra round trip and
prepares nothing: **a 20% regression that looks like a performance change**.

Both keys are therefore set together in `settings/base.py`, and
`dashboard/tests/hypothesis/test_statements_are_prepared.py` asks `pg_prepared_statements` whether
the server is really holding one rather than trusting the settings dict.

## The exception: one query is 55% of all planning, and preparing it does nothing

| | calls | planning | execution |
|---|---|---|---|
| `search_local`'s pin query | 14,499 | 1,973,313 ms (**136.1 ms/call**) | 1,530,200 ms |

That is 0.32% of the calls and **55.0% of every millisecond this database has spent planning**. The
statement is the pin search in `services/map_pins/autocomplete.py:72-87`, which serves
`map.autocomplete.local`: nine joined relations, `SELECT DISTINCT` over ~180 columns, and eight
`icontains` predicates ORed together. It is the reason P100 read 198.9 ms of SQL for that endpoint
— most of that is the planner, not the search.

**Fixed 2026-09-21 in `e8485f72b`, by removing the shape rather than by tuning the planner.** The
`select_related` moved off the matching query onto a second statement keyed by primary key, which
took planning for this statement from 161.6 ms to 2.9 + 1.0 ms. `force_generic_plan` below is
therefore no longer the only lever, and it was never the better one: it makes the planner stop
re-deciding a bad shape, where the split makes the shape cheap to decide. See P100 in
[`archive/PROBLEMS-ARCHIVE.md`](../archive/PROBLEMS-ARCHIVE.md).

Prepared statements do not fix it. Measured directly, 12 `EXECUTE`s of the prepared statement on
one session:

| plan_cache_mode | planning, executions 2+ | median wall |
|---|---|---|
| `auto` (the default) | 73–152 ms, every time | 282 ms |
| `force_generic_plan` | **0.035–0.064 ms** | 198 ms |

Postgres's `auto` heuristic re-plans this statement forever because the generic plan's *estimated*
cost is worse — eight `LIKE $n` predicates against an unknown pattern estimate badly. Its *actual*
cost is the same (198 ms execution either way). So the 84 ms is pure waste that only
`plan_cache_mode` can remove, and psycopg3 alone does not.

Not acted on in this session: `force_generic_plan` set globally would freeze plans for queries
where the parameter values genuinely do change the right plan, and nothing here has measured that
risk. The narrow version — set it for this statement's connection only, or make the query stop
needing nine joins — is unmeasured.

## The instrument is not in the repository

`pg_stat_statements` exists on `ul_perf_db` because someone ran `ALTER SYSTEM` by hand on
2026-09-15. Nothing in `docker-compose.yml` or `src/urbanlens/config/postgres/` mentions it, so
every figure above is unreproducible on a fresh stack until that changes. Neither is any tuning:
the server runs stock (`shared_buffers` 128 MB against a 1,000 MB database, `random_page_cost` 4 on
an SSD, `effective_cache_size` 4 GB against a 2 GB container limit).

## Not measured

- Whether the 2.9x pgbench figure survives end to end through Django, gunicorn and nginx. pgbench
  removes the application entirely; the application's own per-request CPU (158 ms at u500, X26) is
  unaffected by any of this.
- The effect of any Postgres tuning parameter. The stock settings above are reported as facts about
  the deployment, not as a measured cost.
- Whether `prepare_threshold = 5` is the right number. It is psycopg's own default and was not
  compared against any other value.
- Anything about `damballa`. Every figure here is from the local `ul_perf` stack on chiron.
