# D16 — Dragonfly, RabbitMQ and pgvector are adopted now because each already had a live problem to solve, not held for a later one

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D16` · `status: accepted` · `updated: 2026-09-16`

Built 2026-09-16 across three commits on `release/v_0_8_0`: `089114a9c` (Dragonfly), `67a990ee2`
(RabbitMQ), `d8ef1cdfe` (pgvector). All three were on Jess's standing "likely" list for future
stack additions, with the instruction to adopt one early wherever it solves a problem already being
worked rather than shipping a temporary fix it would later replace. This batch is the proactive
case, not a bug fix forced into one of the three: it was requested directly ("add Dragonfly
replacing Valkey, and RabbitMQ, and pgvector"), and it resolves H54 (below) as a consequence of
doing it properly rather than as the reason for doing it.

## What each one replaces or adds

- **Dragonfly replaces Valkey** as the Django cache backend, the Channels layer, and (until the next
  commit) the Celery broker. Wire-compatible over the Redis protocol, so `core/cache_backend.py`'s
  `ResilientRedisCache`, every `cache.get`/`cache.set` call site, and the Channels `RedisChannelLayer`
  config needed no changes beyond the name. `docker-compose.yml:695-734`.
- **RabbitMQ takes over the Celery broker role** that Dragonfly (and Valkey before it) used to share
  with sessions, the Channels layer and the Django cache. The result backend stays on Dragonfly — a
  queue and a fast key/value store are different jobs, and splitting them means a full broker can no
  longer touch cache/session data and a full cache can no longer touch task delivery.
  `docker-compose.yml:736-773`.
- **pgvector adds a vector column type and `<->` distance operators** to the existing Postgres image,
  for future embedding-backed features (nothing in this codebase writes a `vector` column yet — the
  extension is enabled and unused). `src/urbanlens/config/postgres/Dockerfile`.

## Three findings, measured against real running containers, not assumed

**1. Dragonfly runs without `--cache_mode`, and that is deliberate — the two modes are different
failure modes, not a performance knob.** With `cache_mode` on, Dragonfly evicts silently near
`maxmemory` instead of raising once genuinely full. Without it, a full store raises
`OutOfMemoryError`, the same exception Valkey's `volatile-lru` produced on a full write and the
exception `core/cache_backend.py`'s `_REFUSED` tuple (`src/urbanlens/core/cache_backend.py:66-77`)
and `test_cache_outage_is_survivable.py` are written and tested against. Verified empirically before
deciding, with raw RESP-protocol socket writes against a real instance: a 256MB-limited Dragonfly
with `cache_mode=true` accepted 400×1MB writes with zero errors (silent eviction, nothing raised);
the same instance without `cache_mode` correctly raised `OutOfMemoryError` on the write that didn't
fit. Not re-run this session as a regression check — the compose file's own comment at
`docker-compose.yml:702-708` is the durable record of the choice and cites this reasoning.

**2. A full Dragonfly store is a refusal, not an LRU eviction — several existing comments were wrong
about this, for Dragonfly specifically, and got corrected while writing the RabbitMQ commit.**
"Evicts other people's sessions" was accurate wording for Valkey's actual `volatile-lru` behavior,
but was never accurate for Dragonfly, which (per finding 1) raises rather than evicts when full and
run without `cache_mode`. The rename commit carried the old wording forward by habit; the RabbitMQ
commit caught and corrected it in four places: `src/urbanlens/dashboard/services/core/bounded_cache.py`
(module docstring), `test_basemap_tile_cache_is_bounded.py`, `test_saved_filter_cache_does_not_strand_copies.py`,
and `test_redata_media_proxy_limits.py`. The corrected claim: a body too large to fit turns into a
failed cache write for whoever hits it (caught and served uncached by `bounded_cache.set_if_small`),
not an eviction of unrelated keys.

**3. RabbitMQ is pinned to `3.13-management-alpine`, not `4.x`, because 4.x refuses to start a Celery
worker at all.** Celery/kombu's `pyamqp` transport declares its pidbox control queue transient and
non-exclusive, a combination RabbitMQ 4.0 deprecated and now refuses outright:
`INTERNAL_ERROR (541) - Feature transient_nonexcl_queues is deprecated`, crash-looping every worker
on connect. Found by actually starting a worker against a live RabbitMQ 4 instance — this is not
discoverable from the RabbitMQ or Celery changelogs, which document the deprecation but not that
kombu's default pidbox declaration trips it. `docker-compose.yml:736-741` carries the reasoning
inline; CI pins the same image (`.github/workflows/ci.yml`, RabbitMQ service).

## pgvector: layered onto postgis, not built from scratch, and enabled via migration not initdb

`config/postgres/Dockerfile` (`src/urbanlens/config/postgres/Dockerfile`) is `FROM
postgis/postgis:17-3.5` plus `apt-get install postgresql-17-pgvector` — postgis already configures
the PGDG apt repo pgvector ships from, so this is one package away rather than a from-scratch image.
Both `db` and `test-db` in `docker-compose.yml` build from it. The Dockerfile pins no version, so
this drifts with the PGDG repo; checked against the built `urbanlens_development_main_db` container
this session with `dpkg -l postgresql-17-pgvector`, which resolved `0.8.6-1.pgdg11+1`.

The extension is enabled by `src/urbanlens/dashboard/migrations/0048_pgvector_extension.py`
(`RunSQL("CREATE EXTENSION IF NOT EXISTS vector")`), matching the exact pattern
`0001_initial.py:58` used to enable postgis itself — deliberately not postgis's
`docker-entrypoint-initdb.d` hook, since that only runs against a fresh volume and would not reach
the already-initialized dev, staging or production databases.

CI's postgres service is pinned at major version 16, one behind `docker-compose.yml`'s 17
(pre-existing drift, unrelated to this change, left alone). A GitHub Actions service container can't
take a custom Dockerfile the way `docker-compose`'s `build:` can, so CI installs
`postgresql-16-pgvector` into the already-running service container instead
(`.github/workflows/ci.yml`, "Install pgvector into the postgres service container").

Verified against a real running dev database: `CREATE EXTENSION vector` succeeds, a `vector(3)`
column takes inserts, and a `<->` distance-ordered query returns rows in the correct order. No
application code uses it yet — this is infrastructure ahead of the feature that will need it, per
the standing instruction, not a feature landing today.

## The broker/result-backend URL precedence (`settings/base.py`)

```
DRAGONFLY_URL   = UL_DRAGONFLY_URL or UL_VALKEY_URL or UL_REDIS_URL         (base.py:229)
RABBITMQ_URL    = UL_RABBITMQ_URL                                          (base.py:280)
CELERY_BROKER_URL      = UL_CELERY_BROKER_URL or RABBITMQ_URL or DRAGONFLY_URL
                          or "redis://localhost:6379/0"                    (base.py:283)
CELERY_RESULT_BACKEND  = UL_CELERY_RESULT_BACKEND or DRAGONFLY_URL
                          or CELERY_BROKER_URL                             (base.py:284)
```

The broker prefers RabbitMQ over Dragonfly now; the result backend still prefers Dragonfly over
whatever the broker turned out to be, so a RabbitMQ-only environment without a configured Dragonfly
would still work (result backend falls back to the broker) but loses the "fast key/value store for
results" property this split exists for. `UL_VALKEY_URL`/`UL_REDIS_URL` are still honored as
Dragonfly aliases so an unrenamed `.env` does not silently lose its cache.

## End-to-end verification performed during the implementation session

Per the three commit messages, every claim above that says "verified" was checked against real
running containers, not just a passing test suite:

- Django cache reads and writes against a live Dragonfly container.
- `app.control.ping()` broadcast to real Celery workers over RabbitMQ, with pong replies from every
  worker.
- A real `.delay()`-dispatched task, with its result read back from Dragonfly.
- `CREATE EXTENSION vector`, a `vector(3)` column, and a `<->`-ordered query against the live dev
  database.

**This documentation session independently re-checked one of those**: `docker exec
urbanlens_development_main_db dpkg -l postgresql-17-pgvector` against the running dev database
container, confirming `0.8.6-1.pgdg11+1` is actually installed rather than merely requested by the
Dockerfile. The other bullets above were not re-run this session — they are reported from the commit
messages, not independently reproduced here.

**Not measured by either session:** the Celery redelivery-on-failure behavior documented in
`docs/NOTES.md` (~line 707) was traced against the Redis-family transport, where `visibility_timeout`
and the requeue are kombu-emulated rather than broker-native. Native AMQP `basic.reject`/`basic.nack`
redelivery under RabbitMQ has not been traced the same way; treat that mechanism as unconfirmed under
the new broker until it is.

## What this resolves: H54

**H54** (`docs/PROBLEMS.md`, inside the P113 entry) was: *"One 512MB Valkey holds sessions, the
Channels layer, the Django cache and the Celery broker in a single keyspace under `volatile-lru`,
and the Celery broker's keys are the only ones with no TTL — so a user-driven task backlog is paid
for by everyone else's cache and session traffic."* Moving the broker to RabbitMQ removes the
broker's unbounded, no-TTL keys from the shared keyspace entirely — there is no longer a shared
keyspace between the broker and everything else. H54 is closed by this change; see
`docs/archive/PROBLEMS-ARCHIVE.md` ("P113's 65-finding availability sweep") for the resolution
narrative and `docs/PROBLEMS.md` (P113) for the updated status board.

H35 and H38 are a different problem (unbounded per-key body *size* written into the shared store,
not the broker sharing the keyspace at all) and are not resolved by this. H35's size half was
already fixed 2026-09-12 (`bounded_cache.set_if_small`); its key-count half was named against "H54's
Valkey split" in the original audit (`docs/notes/availability-audit-2026-09-11.md`), on the theory
that splitting the shared instance would shrink the blast radius. That theory is only partly borne
out: the broker is no longer in the shared keyspace, so an oversized tile write can no longer starve
task delivery, but sessions and the Channels layer are still on the same Dragonfly instance as the
tile cache, so H35's core risk (one cache write affecting another user's session) is unchanged.
Left open, not resolved, per H35's own live status.
