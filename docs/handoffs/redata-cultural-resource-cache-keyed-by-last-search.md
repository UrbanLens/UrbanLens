# A cultural-resource search's cached answer is the rows last found from that point, so a search from a neighbouring point takes a listing away from it

- **Status: ANSWERED 2026-10-05: fixed on REData `release/0.3.0` (`ba890af5`, `e9f83ffb`, `bfb47503`) and deployed to its staging, where the register check passes for all four primary campuses. Production REData is still 5aabe887.** Written for UrbanLens P286 (`docs/PROBLEMS.md`). Measured against
  `https://redata.urbanlens.org` with the development key on 2026-10-04; REData read from `main` (`45faeb36`).
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What UrbanLens saw

Hudson River State Hospital's campus pin (41.73328, -73.92812) never got its National Register link. Three
requests to `GET /cultural-resources/lookup/?provider=nps_nrhp`, one after another, all from that point:

| request | answer |
|---|---|
| default | `Roosevelt, Isaac, House` (93000857) only |
| `force_refresh=true` | `Hudson River State Hospital, Main Building` (89001166) only, its polygon holding the point |
| default again | both |

The first answer is missing the listing the point stands in. The third still holds Isaac Roosevelt House, which is
about 540 m off and which the live search, at the 250 m default, did not find.

## Why, from reading `main`

- `nrhp.lookup.find_nrhp_resources_near` upserts each feature on `(provider, resource_type, external_id)` and sets
  `latitude`/`longitude` to the point searched from.
- `cultural_resources.cached_point.fresh_cached_resources_near` answers a point with
  `CulturalResource.objects.filter(provider=..., latitude=lat, longitude=lng)`.

So a resource belongs to whichever point found it last. Two consequences:

1. **A row is lost.** A search from a neighbouring point (another building on the same campus, a building pin
   nested under the campus) moves the shared listing to that point. The first point keeps a fresh cached answer,
   because another of its rows is still there, and that answer no longer holds the listing until its TTL runs out.
   The answer is only refetched live once *every* row has moved away.
2. **A stale row is kept.** A row the last live search from a point did not find keeps answering for that point
   until something else finds it. `cached_point.py`'s docstring says this about withdrawn rows; it applies equally to
   a row from an earlier, wider search.

59 provider modules on `main` read through `fresh_cached_resources_near`. CRIS's own lookup
(`cultural_resources.lookup`, line 214) uses the same exact-point filter, but adds the site boundaries containing the
point, which covers a campus's own district but not its buildings.

## What would fix it

The answer for a point has to be recorded against the search, not against the rows. For example:

- A `CulturalResourceSearch(provider, latitude, longitude, searched_at)` row per live search, with a many-to-many to
  the resources it found. The cached read returns that search's resources, and `searched_at` replaces the
  newest-row and negative-marker timestamps.
- Or, without a migration, keep the found ids beside the "searched here" marker `negative_cache.record_search_outcome`
  already writes, and fall back to today's read when the marker has been evicted.

Either way `latitude`/`longitude` on the row could keep meaning "last searched from" for anything else that reads it.

## What UrbanLens did meanwhile

Nothing in code. On its development stack it asked once for `force_refresh=true` from HRSH's campus point and
cleared that location's cached Historic Registers row; the link then appeared on the pin and its wiki. A forced
refresh on every lookup would spend NPS's budget, and a check for a missing listing (CRIS names one, REData's answer
lacks it) would cover only New York, and only listings whose names agree.
