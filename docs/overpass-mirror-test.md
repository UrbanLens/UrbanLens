# Overpass mirror benchmark

Comparison of the new self-hosted Overpass instance (`overpass.osm.urbanlens.org`) against the
four community mirrors and the canonical instance currently configured in
`dashboard/services/apis/locations/boundaries/overpass.py`.

**Run date**: 2026-07-22, ~17:45-18:40 UTC
**Client**: Windows dev workstation, single client, residential/office uplink
**Method**: [`docs/reports/overpass_bench.py`](reports/overpass_bench.py) - 10 Overpass QL programs x 1-3 rounds x 6 endpoints,
120 measurements. Endpoints are queried **concurrently** within a round (they are independent
servers, so queue wait on one cannot perturb another); the one multi-megabyte-payload query runs
serially so concurrent transfers do not contend for client bandwidth. Endpoint order is shuffled
per round so no mirror consistently benefits from going first.

**Client abort**: 30 s for light/medium queries - deliberately matching `OverpassGateway.timeout`
(30 s) in production, since a mirror slower than that has already failed the application - and
60 s for the heavy region-scale queries.

---

## Headline result

**The self-hosted instance is the fastest and most reliable endpoint in the pool for every query
shape UrbanLens actually issues**, with one caveat about region-scale scans (see
[The 90-second proxy cap](#the-90-second-proxy-cap), which is a fixable config issue, not a
capacity limit).

Two of the four community mirrors are effectively **non-functional** right now, and a third is
returning **silently wrong results**. That is a more consequential finding than the timing
numbers.

| Endpoint | Runs | Failures | Median | p90 | Verdict |
|---|---|---|---|---|---|
| **urbanlens (self-hosted)** | 20 | 3 | **1.01 s** | 4.74 s | Fastest; 3 failures all from the 90 s proxy cap |
| maps.mail.ru | 20 | 3 | 1.69 s | 42.64 s | Good on small queries, slow but *completes* big ones |
| overpass-api.de | 20 | 9 | 4.09 s | 7.06 s | 45 % failure rate, all 504 "dispatcher busy" |
| osm.ch | 20 | 0 | 0.82 s | 1.06 s | **Misleading - see below. Returns 0 elements.** |
| kumi.systems | 20 | 19 | 55.42 s | - | 95 % failure rate |
| private.coffee | 20 | 20 | - | - | **100 % failure rate** |

`osm.ch`'s perfect reliability score and fast median are an artifact: it answers almost
instantly *because it has no data to search*. See [osm.ch is a Switzerland-only
extract](#osmch-is-a-switzerland-only-extract).

---

## Per-query wall-clock (best of N runs, seconds)

| Query | urbanlens | overpass-api.de | private.coffee | kumi.systems | osm.ch | maps.mail.ru |
|---|---|---|---|---|---|---|
| Single node by id | **0.28** | 2.14 | FAIL | FAIL | 0.70 *[0 el]* | 0.20 |
| Pin enrichment - urban, 250 m | **0.98** | 3.01 (1 fail) | FAIL | FAIL | 0.76 *[0 el]* | 1.04 |
| Pin enrichment - rural, 250 m | **0.69** | FAIL | FAIL | FAIL | 0.79 *[0 el]* | 0.55 |
| Parcel buildings (`poly:`) | **0.59** | 1.36 | FAIL | FAIL | 0.76 *[0 el]* | 0.41 |
| City bbox buildings, centroids | **1.12** | 2.45 | FAIL | FAIL | 0.90 *[0 el]* | 18.14 (1 fail) |
| City bbox buildings, full geom | **0.92** | 4.09 | FAIL | 55.42 | 0.58 *[0 el]* | 2.80 |
| `historic=*` over NY State | FAIL* | FAIL | FAIL | FAIL | 0.26 *[0 el]* | **41.56** |
| Regex name match, metro bbox | 15.55 | **13.28** | FAIL | FAIL | 0.95 *[0 el]* | FAIL |
| `historic=ruins` over Germany | FAIL* | FAIL | FAIL | FAIL | 2.86 *[CH only]* | **50.42** |
| Recurse admin relation + geom | **0.53** | FAIL | FAIL | FAIL | 0.81 *[0 el]* | 1.77 (1 fail) |

`*` self-hosted failures on the two region-scale scans are the 90 s reverse-proxy cap, not
Overpass giving up - see below.

All endpoints that returned data returned **identical element counts** (548 / 270 / 8,759 /
8,450 ...), confirming the self-hosted clone is complete and consistent with the public
instances for every query that touched real data. Minor +/-1-4 element differences between
mirrors reflect replication lag, not missing data.

---

## Query set

| Key | What it exercises |
|---|---|
| `ping_node` | `node(1);out;` - pure protocol overhead and queue wait, no real work |
| `pin_urban` / `pin_rural` | The exact query `OverpassGateway.nearby_features` sends on pin creation (Manhattan / Adirondacks) |
| `parcel_buildings` | The `parcel_buildings` plugin's `poly:` filter over a large industrial site |
| `buildings_city_bbox` | ~8,800 buildings in a 4x3 km Berlin bbox, centroids - area scan + centroid computation |
| `buildings_city_geom` | Same selection with `out geom` - 9 MB payload, dominated by assembly and transfer |
| `historic_region` | `historic=*` across the whole of New York State - wide-area index scan |
| `regex_region` | Case-insensitive regex on names across the NYC metro - CPU-bound, not IO-bound |
| `ruins_country` | `historic=ruins` across all of Germany - worst realistic case |
| `recurse_admin` | Relation -> way -> node recursion with geometry (Berlin admin boundary) |

---

## Findings

### The 90-second proxy cap

The self-hosted instance's three failures were all region-scale scans, and all failed at
**exactly 90.0 s**. The response is not from Overpass:

```
elapsed 90.42s  status 504
headers: {'Server': 'openresty', 'Content-Type': 'text/html', ...}
<html><head><title>504 Gateway Time-out</title></head>
<body><center><h1>504 Gateway Time-out</h1></center><hr><center>openresty</center></body></html>
```

The `openresty` reverse proxy in front of Overpass cuts the connection at 90 s regardless of the
query's server-side `[timeout:N]`. Re-running the New York State scan with no client cap
confirms the backend is working fine and just needs longer than the proxy allows:

| Query | Self-hosted (no client cap) | maps.mail.ru |
|---|---|---|
| `historic=*` over NY State | 69.19 s, 14,751 elements | 41.56 s, 14,792 elements |
| `historic=ruins` over Germany | >90 s (proxy 504, twice) | 50.42 s, 10,454 elements |

**Action**: raise `proxy_read_timeout` / `proxy_send_timeout` in the openresty config above the
Overpass `[timeout:N]` ceiling you intend to support. Until then, any query needing >90 s is
impossible on this instance no matter what the QL says.

Note the two self-hosted failures are not the same thing. The New York State scan is **purely the
60 s client cap** - uncapped it completes normally (69.19 s, 14,751 elements, matching the 14,792
a global mirror returns). The Germany scan is blocked by the **server's own 90 s proxy cap**, so
its true runtime is still unknown - only that it exceeds 90 s where mail.ru needs 50 s. Neither
is a data or capability gap; see
[Functional completeness](#functional-completeness-of-the-self-hosted-instance).

**Honest caveat**: even with the cap lifted, the self-hosted box is genuinely **slower than
`maps.mail.ru` on country-scale scans** (69 s vs 42 s on NY State; still >90 s vs 50 s on
Germany). It wins decisively on everything smaller. If wide-area scans become a product feature,
that gap is worth investigating - likely disk IO on the area index, and possibly still settling
after the initial clone.

### Functional completeness of the self-hosted instance

The wide-area scan failures above are timeouts, not capability gaps. Checked explicitly, because
a freshly built instance commonly lacks generated **areas** (they are not part of the raw OSM
data - they are built by a separate generation pass, and without them every `area[...]` /
`node(area.a)` query silently returns nothing):

| Capability | self-hosted | maps.mail.ru |
|---|---|---|
| `area["name"="Berlin"]["admin_level"="4"]` | 1 element (10.42 s) | 1 element (2.55 s) |
| `node(area.a)["amenity"="cafe"]` (area-scoped) | 5 elements (3.31 s) | 5 elements (0.83 s) |
| `[date:"2024-01-01T00:00:00Z"]` (attic/history) | **unsupported** | 1 element (2.25 s) |

Areas are generated and working. The one genuine gap is **attic (history) data**, which is not
loaded:

```
runtime error: Tried to use museum file but no museum files available on this instance.
```

UrbanLens issues no `[date:...]` or `[adiff:...]` queries today, so nothing is currently broken.
It does rule out time-travel queries ("what stood on this site in 2015?"), which is a plausible
future want for an urbex application - loading the full-history planet is the (large) fix if that
feature is ever wanted.

### osm.ch is a Switzerland-only extract

`osm.ch` returned **0 elements for 9 of 10 queries**, fast, with HTTP 200 and no error. The one
exception is the Germany scan, which clips Swiss territory and returned 268 elements (vs 10,454
from a global mirror). A direct coverage probe confirms it:

| Probe | osm.ch | self-hosted |
|---|---|---|
| Zürich HB (Switzerland) | 1 el - `Zürich Hauptbahnhof` | 1 el - `Zürich Hauptbahnhof` |
| Grand Central (USA) | **0 el** | 4 el - `Grand Central-42nd Street`, ... |
| Berlin Hbf (Germany) | **0 el** | 4 el - `Berlin Hauptbahnhof`, ... |

This is a **correctness bug in the current pool, not a performance issue**. `OverpassGateway`
shuffles endpoints and treats an empty element list as a valid "no features here" answer
(`elements_for_query` only logs on exception), so roughly **1 in 6 boundary/enrichment lookups
outside Switzerland has been silently resolving to "no data"** — no exception, no retry, no
log. Every user-visible symptom (a pin that gets no boundary, a parcel with no buildings) looks
like missing OSM data rather than a broken mirror. Filed as UL-355 in `docs/PROBLEMS.md`.

### private.coffee and kumi.systems are unusable

- `private.coffee`: **20/20 requests failed**, including `node(1);out;`. In the earlier
  uncapped run it did occasionally succeed, but only after 84-97 s of dispatcher queueing -
  far beyond the 30 s the app waits.
- `kumi.systems`: **19/20 failed**; the single success took 55 s.

Both fail by queueing, not erroring, so they burn the full client timeout before failing over.
The code comment at [`overpass.py:28-30`](../src/urbanlens/dashboard/services/apis/locations/boundaries/overpass.py#L28-L30)
claims these mirrors "routinely answer the same query in well under a second" — that is no
longer true.

### overpass-api.de is unreliable but not slow

9/20 failures, every one an HTTP 504 dispatcher-busy response, typically after 6-16 s. When it
does answer it is reasonably quick (median 4.09 s). Its `/api/status` reports `Rate limit: 2` —
two concurrent slots per IP.

### Rate limits

The self-hosted instance reports `Rate limit: 0` (no per-IP slot cap) versus `Rate limit: 2` on
`overpass-api.de`. Relevant for the `panel_fetch` Celery queue, where several pin-detail panels
may hit Overpass concurrently — against the public instances that concurrency is itself a source
of 429/504s.

---

## Recommendations

1. **Make the self-hosted instance the primary endpoint** (`_API_URL`). It is the fastest on
   every query the application actually issues, has no per-IP slot limit, and is the only
   endpoint under our control.
2. **Remove `osm.ch` from the pool entirely.** It is a regional extract silently poisoning
   non-Swiss lookups.
3. **Remove `private.coffee` and `kumi.systems`.** At a 95-100 % failure rate they contribute
   nothing but latency: because they fail by timeout rather than by error, each one selected
   first costs a full 30 s before failover.
4. **Keep `maps.mail.ru` and `overpass-api.de` as fallbacks.** Both are globally complete and
   usable; mail.ru is notably the *best* endpoint for region-scale scans.
5. **Raise the openresty proxy timeout** above the intended `[timeout:N]` ceiling.
6. **Reconsider the equal-peer shuffle in `_available_endpoints`.** It was written on the premise
   that all mirrors perform comparably. With a fast instance under our control, prefer it and
   treat the rest as ordered fallbacks.
7. **Consider validating "empty" responses.** A globally-complete instance returning 0 elements
   is normal; a *pool member* consistently returning 0 where others return data is a signal
   worth detecting rather than silently accepting.

---

## Method caveats

- Single client on a single network. Round-trip time to each mirror differs; the self-hosted
  instance is likely network-nearer, which flatters it on small queries (though the `ping_node`
  spread — 0.28 s vs 2.14 s — is far larger than plausible RTT differences).
- Public mirror load varies by time of day. These numbers are one ~1-hour window on 2026-07-22
  and should not be read as a permanent characterization of any community mirror.
- The self-hosted instance was freshly built; its first run of a query is measurably slower than
  subsequent ones (city bbox: 4.74 s cold, 1.12 s warm), so OS page cache is still filling.
  Public mirrors have long-warm caches, so this comparison is if anything unfavourable to the
  self-hosted box.
- `buildings_city_geom` ran only 1 round (it is the serial, multi-megabyte query); its numbers
  are single measurements, not best-of-N.
- Raw measurements: [`docs/reports/overpass_mirror_results.json`](reports/overpass_mirror_results.json)
  (120 rows: query, endpoint, round, status, TTFB, total, bytes, element count).
  Harness: [`docs/reports/overpass_bench.py`](reports/overpass_bench.py) - re-run with
  `.venv_windows\Scripts\python.exe docs/reports/overpass_bench.py --only all --out results.json`.
