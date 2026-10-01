# N26 — The audit of the basemap and performance range, what it found, and what measurement took back

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Status: `current` · 2026-09-21. Range audited: `e71e4dde6..615041aa1` — the basemap-tile proxy, the
MapLibre engine, the tile client, the write-source deferral, the capacity harness and the
deployment config, written across 2026-09-19..21 by several Sonnet sessions.

A review sweep over eleven areas produced 30 findings. Every one was checked against the code, and
several against a live system, before anything was changed. 23 became changes; five were closed
with a number or a decision instead; one was refuted outright and one had its reason refuted after
the change had been made; one agent's fix pointed the wrong way; one is open. What follows is the
disposition, then the parts worth carrying forward.

## Disposition

| # | area | what it became |
|---|---|---|
| 1 | tile proxy 500s on an oversized body | fixed, `6888837d4` |
| 2 | week-long browser cache outlives the revocation window | **accepted, documented** — see below |
| 3 | upstream `Content-Type` trusted | fixed, `3f1fd287b` (allow-list, `nosniff`, uncached refusal) |
| 4 | the historical-map proxy got none of the hardening | fixed, `3f1fd287b` |
| 5 | no coalescing on the cold auth path | **declined, documented** — see below |
| 6 | catalogue refill has no stampede protection | fixed, `3d489af1a` (`single_flight`) |
| 7 | tile cache shares a 1 GB Dragonfly with sessions and Channels | fixed, `da3f6c885` (own store) |
| 8 | sentinel coordinates corrupt a layer id containing them | fixed, `f8d208de4` |
| 9 | the external API still resolves the profile on every request | **changed, saving refuted** - see below |
| 10 | no invariant check would catch a write-source misattribution | fixed, this session — one rule, plus a check for a second copy of it |
| 11 | `Retry-After` clamp collapses jitter | fixed, `6888837d4` (47% of first retries fired at exactly 1000 ms) |
| 12 | a queued tile is not re-checked against the viewport | fixed, `f8d208de4` |
| 13 | `sleep()`'s abort listener leaks | fixed, `f8d208de4` |
| 14 | MapLibre attribution hardcodes the vendor credit | fixed, `6888837d4` — a licence error, not a cosmetic one |
| 15 | one WebGL context per thumbnail, uncapped | fixed, `747b8e4bd` |
| 16 | no MapLibre equivalent of `errorTileUrl` | fixed, `a565a713d` |
| 17 | borders opacity duplicated as a literal | fixed, `bbf7a68b4` |
| 18 | `isMaplibreMap()` duplicated | fixed, `bbf7a68b4`, **in the opposite direction to the one proposed** — see below |
| 19 | `supportsWebGL2()` never cached | fixed, `bbf7a68b4` |
| 20 | the MapLibre marker/cluster facade has no consumers | **confirmed, documented** — doc comments and PL8 now say it is unwired |
| 21 | no caller proves the engine-neutral contract out | open, same cause as 20 |
| 22 | the harness's browser-cache model suppressed 73% of its own tile load | fixed, `615041aa1` |
| 23 | neither deployed environment runs the config the capacity work describes | **confirmed on damballa, not changed** — see below |
| 24 | the connection-budget test never reads `production.sample.env` | fixed, this session |
| 25 | Postgres is entirely stock | fixed, `c46b0c9f5` + this session's `production.sample.env` sizing from X28 |
| 26 | nginx proxies with no keepalive | **closed by measurement** — 0.24 ms a request, 0.02% of it |
| 27 | the "one round trip" tile cache read has no test | fixed, this session |
| 28 | P131 asserts a PBKDF2 cost REData has already removed | confirmed by live measurement, P131 rewritten, `ac4dd7013` |
| 29 | `INDEX.md`'s next-free id was stale | fixed |
| 30 | the `vector_layer_not_served` branch is dead code | **refuted by live measurement** — see below |

## What measurement took back

**#30 is refuted.** REData's `T9` told us the `_VECTOR_LAYER_REFUSAL` branch was dead, and a comment
in `basemap_tiles.py` had already been written repeating that. Production disagrees:
`GET https://redata.urbanlens.org/api/v1/tiles/street/14/4823/6037/` answers `400
vector_layer_not_served` in 0.10 s today, and the catalogue publishes `street` and `dark` as
`source_type: "vector"` with a `style_url` and no `url_template`. Removing the branch would have sent
that answer down the definitive-404 path and cached a week-long hole in the base layer. The comment
was corrected to say the branch is live, not defensive.

**#2 is accepted, not overlooked.** `TILE_AUTH_TTL` bounds a *fetch*, not a pixel: a browser that
already holds a tile keeps rendering it for up to the week `immutable` promises, and a revocation
does not change the session cookie the response varies on. The bytes are public vendor imagery,
proxied so the vendor never learns which coordinates a viewer is looking at, and they carry nothing
about the account that fetched them. Shortening the header would re-fetch every tile a viewer has
already seen, which is the cost the module exists to remove. `tile_authorisation.py`'s docstring now
states the real worst case rather than the TTL alone.

**#18's fix pointed the wrong way.** The agent moved `isMaplibreMap()` into `maplibre-layers.ts` and
checked for import cycles, which there were none of. What it did not check was weight:
`map-view.ts` is a leaf the marker and cluster facades import, and the move would have pulled
`maplibre-layers.ts` and its whole tree — pmtiles included — into every consumer of it. The
definition lives in `map-view.ts`, which imports nothing, and `maplibre-layers.ts` re-exports it.

**#9's saving does not exist.** The external API's eager binding was made lazy, so the two
implementations of one rule agree again - they had drifted once already. But the query it was
supposed to save is paid regardless: `ApiKeyAuthentication` resolves its key with
`select_related("user", "user__profile")`, and on the OAuth2 path (`select_related("application",
"user")`, no profile) the view reads `request.user.profile` for its own filtering. Measured on both
authenticators, a read costs exactly one `dashboard_profiles` query either way. The test asserts the
ceiling rather than the absence, and says why.

**#5 is declined on the numbers.** Coalescing the cold viewport's auth check would make 29 requests
wait on a 30th to save 29 indexed `auth_user` lookups. The latency costs more than the queries. The
test's comments claimed "one check instead of 30" without the concurrent caveat; they now say a
session pays one check per TTL but its *first* viewport can pay up to one per concurrent tile.

## What the audit's own fix broke

`da3f6c885` gave proxied bytes their own cache alias. `seed_basemap_tile_cache` kept writing to
`default`, so the perf environment's tile cache was empty for a run that exists to measure a warm
one — the first capacity ladder failed pre-flight with 12 of 12 sampled tiles answering 503.

The tests did not catch it because `settings/test.py` gave both aliases a `LocMemCache` with the
same `LOCATION`, and `LocMemCache` shares storage by `LOCATION`, not by alias. The two aliases were
one store under test, which made four existing assertions vacuous — including two that were meant to
prove a revoked session stops being served. Each alias now has its own `LOCATION`, the base
`TestCase` clears every alias rather than `default`, and one test asserts nothing spells a tile key
by hand. Fixed in `f346409c5`.

## Closed by measurement

**#26 — nginx opening a connection per proxied request costs 0.24 ms.** `uct=$upstream_connect_time`
was already in the log format, so the prize was measurable before paying for it. Over the 108,167
proxied requests of the 2026-09-21 ladder (100 → 1,000 users): mean `uct` 0.25 ms against a mean
`rt` of 785 ms — 0.03% of request time — with a maximum of 17 ms and three requests over 10 ms in
the whole run. During the 1,000-user hold alone it is 0.24 ms against 1,128 ms, 0.02%. Adding
keepalive needs an `upstream {}` block, and open-source nginx resolves an `upstream {}` server name
once at startup — the stale-container-IP failure the variable-upstream form here exists to prevent.
Reintroducing that to save a quarter of a millisecond is not a trade worth making. Re-measure if the
tiers ever stop sharing a Docker bridge.

## Still open

- **#23 — damballa runs neither configuration.** Verified read-only on 2026-09-21:
  `urbanlens_production_app` (image built 2026-08-27) runs `gunicorn -k gevent`, `WEB_CONCURRENCY=3`,
  `NanoCpus=0`, `Memory=0`, with `UL_DB_CONN_MAX_AGE` unset; `urbanlens_staging_app` (built
  2026-09-12) is the same with `--worker-connections 20`. The CPU/memory half of this is already in
  P125; the worker-class half was not recorded anywhere. **Every capacity figure in P125, X26 and
  X28 describes the `ul_perf_*` stack, not a deployment that exists.** Closing this is a rebuild and
  a recreate on a production host, so it is written down rather than done.
- **#21** — no live consumer of the engine-neutral marker contract, same cause as #20.

## Closed after the audit

**#10 — the rule naming a request's writer now has one definition.** The site's middleware and
`ExternalApiView.initial()` each held their own copy of "USER if signed in, else SYSTEM, actor is
the profile pk". They had drifted apart once (#9), and nothing failed when they did: a write
attributed to SYSTEM that a user actually made is a row that looks correct everywhere except in
what it means, and each path's own tests pass because each path is self-consistent. Both now call
`versioning.request_writer()`, and a test walks `src/urbanlens` for any other module deciding a
`WriteSource` from `is_authenticated` — verified non-vacuous by adding a third forked entry point
and watching it name the file. Consumers were checked for the same gap: `consumers.py` performs no
writes, so daphne's not running Django's HTTP middleware costs nothing here.

**#25 — `production.sample.env` now sizes the database.** X28 supplied the numbers the sizing was
waiting on: `CPU_LIMIT__DB=6`, `MEM_LIMIT__DB=4g`, `UL_DB_SHARED_BUFFERS=1GB`,
`UL_DB_EFFECTIVE_CACHE_SIZE=3GB`, alongside an app tier raised to the 1,000-user extrapolation
(`CPU_LIMIT__APP=8`, `WEB_CONCURRENCY=12`, `MEM_LIMIT__APP=4g`). Twelve workers × 4 gthread threads
is 48 held connections against `ul_web`'s `CONNECTION LIMIT` of 54, so the worker count has a
ceiling of 13 before the role budget — which sums to exactly the 97 a stock Postgres leaves
non-superusers — is the binding constraint rather than CPU. `test_connection_budget_wiring.py`
checks that arithmetic against every env sample, which is #24's fix earning its place.

Verifying it turned up the sharper half of #23: `urbanlens_production_db` runs **bare `postgres`**
with no arguments at all (read-only inspect, 2026-09-21), so production has neither the cgroup
limits nor `jit=off`, `shared_preload_libraries` or a `shared_buffers` above the stock 128MB. The
command is fixed at container creation, so this needs a recreate rather than a restart — the same
production-host operation #23 is written down instead of done.

## Related

P125, P131, X26, X27, X28, PL8, N25.
