# REData production failed UrbanLens's 57-campus live-locations run in seven ways of its own, the worst a Smithsonian parse error that fails archive search for 19 campuses

- **Status: PARTLY ANSWERED 2026-10-07: items 1 and 6 by REData 0.3.7, pending release.** REData's reply is its T15
  (`docs/urbanlens-2026-10-07-replies.md` on its `release/0.3.7`, `d1f1bbcb`). Item 1: an archive provider that raises
  is isolated, reported `unavailable` with `complete: false`, and the other archives' results still come back;
  Smithsonian reads an empty date list. Item 6: `/search/news/` carries `complete`, `degraded` and a per-provider
  `providers`, with `complete: false` whenever GDELT did not answer. Neither is released or deployed; production runs
  v0.3.6. UrbanLens reads both on `release/v_0_9_0` (branch `fix/redata-partial-answers`): a partial news or archive
  answer is shown and kept an hour, an empty one is not kept, and an older REData's answer is read as before.
  REData #147 on the same release answers item 2's cold parcels with 503 `refresh_queued` and a `retry_after`;
  UrbanLens waits that out on the same branch. Originally SENT 2026-10-07. Measured against `https://redata.urbanlens.org` with UrbanLens production's key,
  2026-10-07 01:09-05:06Z. Production ran REData v0.3.5 until 03:05:28Z and v0.3.6 after; each item says which. Line
  numbers are REData `v0.3.6`'s.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.
- `id: N47` · `status: current`

`bin/run_live_location_tests.sh` checked REData's answers for every campus in `tests/live_locations/kirkbrides.toml`,
one session per campus, without retrying a refusal. PL9 (`docs/notes/redata-integration-programme.md`) has the
totals and every failure's class. This file holds only the failures that are REData's to fix. Request ids are
REData's `X-Request-ID`.

## 1. A Smithsonian record with an empty date list fails the whole archive search with a 500

`GET /reference-documents/search/` answered `500 internal_error` for the first label of 19 campuses, on both
versions: Arkansas, Austin, Central State (Indiana, Kentucky and Virginia), Cherokee, Clinton Valley, Columbus,
Eastern Oregon, Eastern State (Washington), Fergus Falls, Harlem Valley, Mendota, Mississippi, Oregon, Southern Ohio,
Trenton, Warren and Western State (Kentucky). Production's log for Harlem Valley (request
`e0a81f64ec934c9eaa48116fa43497ce`) and for Central State Indiana, Trenton and Western State has the same traceback:

```
parcels/services/reference_documents/gateways.py:445, in search   (SmithsonianGateway)
    date_text=str((content.get("indexedStructured") or {}).get("date", [""])[0] or "")[:100],
IndexError: list index out of range
```

`indexedStructured.date` can be an empty list, not only absent. `ProviderRegistry.find_for_query`
(`core/services/provider_registry.py:756`) catches `GatewayUnavailableError`, `RequestCancelledError` and
`ImpossibleInputError` only, so one provider's parse error takes down every other archive's answer to the same
query. Either change alone stops the 500.

## 2. A cold campus's buildings or boundaries answer blocks a gevent worker until gunicorn kills it (P62)

Seven campuses got `502` or `504` from openresty instead of an answer:

| campus | endpoint | what the client saw |
|---|---|---|
| Augusta | `boundaries/`, `buildings/` | `504` at 90 s; `502` three times at about 60 s |
| Austin | `buildings/` | `502` at 61 s, then `200` with 42 rows 35 s later |
| St. Elizabeths | `boundaries/`, `buildings/` | `502` at 51 s and three times at about 39 s |
| Richardson Olmsted | `boundaries/`, `buildings/` | `504` at 90 s, `502` three times at 52-63 s |
| Spring Grove | `reference-documents/search/` | `502` at 87 s |
| Anna, Winnebago | `reference-documents/search/` | `504` at 90 s |

For each `502` production's app log has `[CRITICAL] WORKER TIMEOUT` followed by `Worker (pid:…) was sent
SIGKILL!`. Gunicorn runs gevent workers (`gunicorn.conf.py:5`) with its default timeout, and these requests did not
yield to the event loop for that long. That is REData P62's mechanism, here reached through the buildings and
boundaries routes. Every request sharing a killed worker dies with it, UrbanLens production's included. Nothing
was cached, so the next ask repeats the work. Production logged 1 to 3 `WORKER TIMEOUT`s an hour on 2026-10-06
from 12Z to 00Z, and 7 to 16 an hour while this run was asking. UrbanLens's suite re-asked each failed call
once per check until its `438720f58`. It was also 50 to 77 an hour on 2026-10-06 from 04Z to 11Z, before this
run.

## 3. Production's map catalogue has no volumes for towns loc.gov holds atlases of (P112, on production)

`/maps/` and `/maps/volumes/` returned nothing for four campuses whose town has a Sanborn atlas on loc.gov:

| campus | loc.gov | campus to downtown |
|---|---|---|
| Athens (0.3.5) | 8 editions of Athens OH, 1885-1948 | 1.2 km |
| Central State, Indianapolis | 31 volumes, 1887-1956 (`sanborn02371`) | 4.5 km |
| Jacksonville, IL | 7, 1887-1946 (`sanborn01939`) | 1.6 km |
| Western State, Hopkinsville KY | 9, 1886-1950 (`sanborn03185`) | 3.3 km |

On 0.3.5 production's `imagery/` found six Athens Sanborn layers through `loc_sanborn` in the same minute that
`/maps/volumes/` found none (requests `760d5d7884734bfa8aefa0315f9b82e3` and `ed005e3f4f3a4013a17b65982e670c84`). By
05:05Z, on 0.3.6, `/maps/` answered Athens with 25 sheets (request `65690be61d7043d38c37a2c224ab8848`); the other
three were not re-asked. `catalogued_volumes_near` (`parcels/services/historical_maps/lookup.py:246`) reads only the
harvested catalogue. Staging's targeted harvest for the primary campuses has no production counterpart.

## 4. Parcels and owners

- **Pennsylvania's owner is in fields REData does not read.** Harrisburg State Hospital's parcel `62-026-004` has
  `OWNER_NAME` null and `OWNER_LAST_NAME` / `OWNER_FIRST_NAME` = COMMONWEALTH OF PENNSYLVANIA / DEPARTMENT OF
  GENERAL SERVICES. The PA statewide entry (`parcels/services/property_records/known_endpoints.py:426`) relies on
  the global `OWNER_NAME` candidate.
- **Philadelphia** (The Institute of the Pennsylvania Hospital, `060N110211`): the PA layer carries no owner field at
  all there. The City's own OPA property data names owners.
- **Counties with no source**: 404 `unresearched` for Union County IL, Kane County IL, Jefferson KY, Shawnee KS,
  Oakland County MI, Grand Traverse County MI and St. Louis MO. Jefferson County KY's LOJIC and Oakland County
  publish parcels.
- **Configured, and nothing for the point**: 503 `no_data_found` for Christian County KY (Tier 2), Montour County PA,
  Warren County PA and Nicollet MN (Tier 1). Each is a state hospital's campus point.
- **The vendor tier needs an address REData could not derive**: 503 `search_key_unavailable` for Tuscaloosa AL and
  Miami County KS.
- **Statewide layers with no owner name**, county sources not researched: California (CAL FIRE: Napa, Agnews,
  Mendocino), Maine (Augusta), Virginia (Central State), Washington (Eastern State), and New Jersey's and Maryland's,
  which blank the field (Trenton, Greystone; Spring Grove, Sheppard Pratt).
- **Athens** is fixed: on 0.3.6, after its row was re-pointed and P128's stale marking, the point answers parcel
  `f9d2af97-…` with owner STATE OF OHIO (request `d8e43ecd71084d1f96614f74d362f976`). On 0.3.5 it answered the
  statewide `39009-A029050100100`, with no owner.

## 5. Greystone's National Register listing within 500 m was not returned

`/cultural-resources/lookup/` at Greystone Park (40.834722, -74.505278, 500 m) returned 0 rows from both `nps_nrhp`
and `nj_shpo`. NRHP 00000653, the Illumination Gas Plant of the NJ State Asylum for the Insane, is listed about
400 m away, and NPS's and NJ DEP's own services return it for the same 500 m query. Not verified live: the positive
and negative caches look keyed on provider and point without the radius (`nrhp/lookup.py`
`_get_fresh_cached_resources_near`, `negative_cache.py` `_cache_key`). An earlier empty lookup at the 150 m default
would then answer every later radius. N42 is the same cache's earlier fault.

## 6. A news answer from the SearXNG fallback does not say GDELT was skipped

GDELT answered production with `429` five times during the run, from 01:11Z, and was in back-off or a
provider-health hold for most of it. `/search/news/` then answered `200` from SearXNG's news category, which was 0
rows for 17 campuses, with brave.news and google news throttled. Nothing in those answers says GDELT was not asked
(`provider: "searxng"` only), so a client caches "no news" for a campus. When SearXNG was empty too, the answer was
a `503 search_unavailable`, which is right.

## 7. Wikipedia near a point misses an article whose coordinates are not marked primary

`/reference-documents/?provider=wikipedia` near Eastern Oregon State Hospital (45.6715, -118.817) gave Pendleton,
Blue Mountain Community College and Pendleton High School (498-997 m), not "Eastern Oregon Correctional
Institution". The article has coordinates 7.8 m from the point, but its GeoData flags them as not primary.
Wikipedia's `list=geosearch` with its default `gsprimary=primary` leaves the article out; `gsprimary=all` returns it
first, ahead of exactly those three. The `wikidata` provider, whose P625 is the same point, answered `skipped`.

## 8. Build years outside New York and Athens (P111's class)

27 standing campuses' on-property buildings carry no `year_built` on either version: Agnews, Arkansas, Broughton,
Central State (Indiana, Virginia), Cherokee, Clarinda, Eastern Oregon, Eastern State, Fergus Falls, Greystone,
Harrisburg, Independence, the Institute of the Pennsylvania Hospital, Jacksonville, Kalamazoo, Kankakee, Mendocino,
Mendota, Mississippi, Napa, Oregon, Patton, Terrell, Trans-Allegheny, Winnebago and Athens. REData's rewritten P111
says why for Athens. Nothing has been looked at for the other states.

## 9. Incidents

46 campuses got `count: 0` with every one of 148 providers `not_applicable`: P110's finding for New York and Ohio
holds for every other state the catalogue touches. Covering feeds answered with nothing within 2 km at two:
`louisville_fire` (Central State, Kentucky) and `chesterfield_va_fire_ems` (Central State, Virginia). Passing
campuses were covered by the Indianapolis, Dayton, Austin, Philadelphia, DC, Buffalo, Columbus and Oshkosh feeds.

## Not REData's

The suite's own gaps are UrbanLens#339 ("news check searches a shared campus name without its town") and
UrbanLens#340 ("register check reads only each row's name"). Six register misses were UrbanLens's catalogue naming a
campus differently from its listing, or expecting one the Register had removed; Terrell's point was right, and the
THC marker's own coordinates are the ones outside the grounds (PL9). Image search is thin everywhere because the engines REData relays to
refuse the shared egress IP (P116).
