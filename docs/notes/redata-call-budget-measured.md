# X30 — What one pin, and one campus, cost in REData calls

> **Written by a Claude agent. Not authoritative.** Measured on `development_main`
> on 2026-09-23, before the fix at `1b6ce62f5` and after it at `3680feb84`. Re-run
> before relying on any figure here.

`id: X30` · `status: holds` · `updated: 2026-09-23`

Jess's question: one HRSH run uses up the key's REData lookup budget (1,000 an
hour). Is that only demo/staging, and isn't 1,000 for one suite far too many?

It is not only demo/staging. Every environment's key is throttled the same way:
see P144. And yes, it was far too many. Most of it was calls the stack
made after REData had told it to stop, plus the same question asked several times.

## The HRSH hour, from `ApiCallLog`

19:30-20:30 UTC: the GOALS Playwright suite plus the HRSH location suite made
**1,439 REData calls, and 1,041 of them failed**. The run created one campus pin and
**69 building pins**, each on its own `Location` (70 locations), so it came to about 20 calls per pin.

- REData throttled the key's lookup pool at about 20:02 ("Expected available in
  989 seconds"), and nothing stopped calling. DRF charges a refused request to
  the key's 2,000/hour default pool as well, so the retries kept that pool spent
  too.
- `places/search/nearby` succeeded **4 times out of 1,901** that day. The worker
  logs show mostly REData's own 503 `rate_limited` (REData's Google Places budget
  is out, which is not the key's budget), plus 503 `places_api_unavailable` and
  the key's 429.

## Method

A harness (in the session scratchpad; not committed) patched
`_RateLimitedSession._do_request` to record every REData call's path, params,
status and caller stack. It ran Celery eagerly, so every task ran in the same
process. It created a pin at a site nobody had pinned, rendered the private pin
page with the test client and followed every `hx-get` two levels deep, which is
one viewer. For a campus it then created three building pins as auto-nest does
(`Pin.objects.create`, each on its own `Location` 80-150 m from the root), viewed
each, and viewed the root again. It ran in a throwaway container from the app
image, because other sessions' `sync_app --restart` kept killing a long
`docker exec`. The before run mounted `git archive 1b6ce62f5`; the after run
mounted `3680feb84`.

## One pin, before (Pilgrim State, probe pin 100418)

| phase | calls |
|---|---|
| create | 4 |
| first page view | 46 |
| second page view | 6 |
| **total** | **56**, of which **14 repeat an identical request** |

The repeats:
- `parcels/lookup` ×3 from the REData boundary provider, `parcel_buildings` and `property_records`.
- `capabilities` ×3, because `applicable_providers` cached per domain and three domains asked for one index.
- `places/search/nearby` ×3 from name resolution: twice at creation, once on view.
- `search/web` ×3, the poll storm below.
- `street-view/timeline` ×3 per failing provider.
- `maps/` and `labels/suggest/` once per view.

## A campus, before (Willard, root plus 3 buildings, `1b6ce62f5`)

| phase | calls |
|---|---|
| create root | 4 |
| view root | 53 |
| create 3 buildings | 9 (3 each: two identical 50 m place searches and a parcel lookup) |
| view each building | 51, 52, 49 |
| view root again | 14 |
| **total** | **232** sent: 169 distinct, **63 exact repeats**, 46 answered 429 |

A building's page cost as much as the site's. It re-asked everything, including
the nearest park unit within 100 km and earthquakes within 100 km. It also re-fetched
the campus's own CRIS record: `fetch-detail` ×6 and attachment `extract` (OCR on
REData's side) ×19 for 7 attachments.

## Why the failures multiplied

- **No backoff.** Nothing read REData's 429 `Retry-After`.
- **A poll storm.** A panel source that meets an outage leaves its store empty
  on purpose, so the outage is not cached as "nothing here". Web Images, Google
  Images and site conditions do this, and so do the six `RedataInfoPanelSource`
  panels on an empty incomplete answer. It then
  returned normally, so no skip key was set. The page polls every 2 s, 30 times
  per viewer, and every poll dispatched the same failing call again.

## What changed

| commit | change |
|---|---|
| `6fc9b6cc1`, `d723afe20` | A shared breaker (`services/core/upstream_breaker.py`). A 429 opens the pool the endpoint draws from (lookup, default, tiles or a write pool, classified from REData main's `throttle_classes`). A 503 `rate_limited` opens only that endpoint plus the provider it named. Until the retry-at time held in Dragonfly, every process refuses the call without a request, logs `was_rate_limited=True`, and raises `UpstreamThrottledError`. It is a `RateLimitExceededError`, `UpstreamBusyError` and `GatewayRateLimitedError`, and gateways whose callers catch their own types get `PropertyRecordsBusyError` or `LocationContextBusyError` instead. |
| `b200e2014` | A panel fetch that returns with the source still not ready is suppressed for the failure window, not re-dispatched on the next poll. An `UpstreamBusyError` is suppressed for exactly the wait REData named. |
| `e3e4706f6`, `3680feb84` | `coalesced()` (`services/core/coalesce.py`): the first caller's answer is shared through the cache, and callers already in flight wait for it. Applied to parcel lookup, the capability index (per 0.01° cell), `parks/nearby` (asked at the cell centre), Places nearby/details, and CRIS fetch-detail and extract. |
| `982e15c2b`, `5c82d2d6f` | `site_level` panel sources. A pin nested under a site, within 1 km of it, copies the site's fresh answer. When the site has none, one coalesced fetch asks for the site. Flagged: nps, hazard_history, usgs_earthquakes, air quality, incidents, hydrology, underground, site conditions, and property_records (only when the pin is inside the site's parcel geometry). |

## After (Middletown, root plus 3 buildings, `3680feb84`)

The dev key was already throttled when this ran. In the preceding 70 minutes
the stack's other sessions sent REData 1,420 calls (of 2,398 attempts) and kept
the lookup pool at its limit. So the run mostly measured the breaker:

| | before | after |
|---|---|---|
| requests that reached REData | **232** | **41** |
| attempts refused without a request | 0 | 194 |
| distinct (path, params) asked | 169 | **111** |
| answered 429 | 46 | 5 |

The distinct count is the one that stands in for fan-out. Coalescing and site
sharing replace a building's area questions with the site's, so the after run
asked 34% fewer distinct questions at a comparable site. It is a lower bound on
what the code sends with headroom, not the count itself: `street-view/timeline`,
`maps/`, `labels/suggest/` and `search/web` are not coalesced and still repeat
per page view.

For the stack as a whole, `ApiCallLog` for 22:21-22:24 recorded 772 REData
attempts, and 621 of them were refused before sending. The log does not say
whether the breaker or UrbanLens's own per-service limit refused each one.

## Not measured

- The after-campus with a key that has headroom: that needs an hour with no other
  sessions on the dev key.
- Production's per-hour volume: its key is shared with staging and both k3s
  namespaces (P144), so its budget has to cover all four.
