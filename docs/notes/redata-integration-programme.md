# PL9 — Every REData answer UrbanLens can use, surfaced, cached for as long as it is true, and checked against real campuses

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL9` · `status: live` · `updated: 2026-10-07`

## What done means

- UrbanLens ingests and shows nearly every REData endpoint that has a use here, and works without REData at all.
- A REData answer is cached for as long as it is true: a partial answer briefly, an outage not at all.
- A pin dropped on a historic Kirkbride campus becomes the whole property: the parcel on the top pin and its wiki,
  a child pin and child wiki per building with its outline, build dates, owner, register listing, Wikipedia,
  archives, photos, web and news results, incidents and historic maps.
- `tests/live_locations/` checks all of that against a real REData for the four primary campuses (HRSH,
  St. Lawrence, Harlem Valley, Athens) and the rest of the 57 in `kirkbrides.toml`. See `LOCATION_DATA_TESTS.md`.

## Where it stands (2026-10-07, REData production on v0.3.5)

`bin/run_live_location_tests.sh` against REData production (`https://redata.urbanlens.org`, v0.3.5) with
UrbanLens production's own key, 2026-10-07 01:09-01:15Z. Production was chosen so the answers are cached for real
requests, and staging's share stays where it is. The run used one pytest session per campus, 60 s apart, and
`UL_LIVE_MAX_WAIT_SECONDS=0`, so no refusal was retried. It stopped at the first REData refusal.

| Group | Run | Passed | Failed | Inconclusive |
|---|---|---|---|---|
| HRSH and Athens, production 0.3.5 | 32 checks | 26 | 6 | 0 |
| the same two, staging 0.3.5 (2026-10-06) | 32 checks | 28 | 4 | 0 |
| all four primaries, staging 0.3.5 (2026-10-06) | 64 checks | 51 | 10 | 3 |
| all four primaries, staging 0.3.0 (2026-10-05) | 64 checks | 43 | 15 | 6 |
| St. Lawrence and Harlem Valley, production | not run | | | |
| the other 53 campuses, production | not run | | | |

HRSH matches staging: 14 passed, and its two failures are its `known_issues` entries. Athens drops from 14 to 12. Its
`build_dates` and `incidents` failures are `known_issues` entries, as on staging. `ownership` and `historic_maps`
are new: both passed on staging, and both are production's REData data, not UrbanLens. No `known_issues` entry
passed, so `kirkbrides.toml` is unchanged.

**Why it stopped.** At Athens, the second campus, production answered `media/lookup/` with flickr and instagram
`rate_limited`. The message was "Media metasearch budget is exhausted right now ... `searxng_media`" (request
`05a44f075d554675b217fa71df6398b4`). It answered `imagery/` with map_warper `rate_limited` (request
`760d5d7884734bfa8aefa0315f9b82e3`). Both answers were partial, and Athens's photos and imagery checks still passed on
the other providers. HRSH's media lookup came from cache in 0.4 s, so Athens's was the run's only `searxng_media`
fan-out. Its default production window is 30 a minute and 3,000 a day, times 0.9. This run's share of it was that one
lookup, so what spent it was production's background media sweeps (REData P108), UrbanLens production, or the
lookup's own page-walk, which is tens of calls a platform.
The message does not say which window ran out, and production's rate-limit state was not read. There was no `429`,
no `key_budget_exhausted` and no `503`. The run made 33 REData requests, plus 4 diagnostic ones: `capabilities/`,
`schema/` and two reads of the Athens parcel.

What still fails on production:

| Check | Site | Why | Tracked |
|---|---|---|---|
| ownership | Athens | `owners/` and `assessments/` are empty. The point resolves to the cached parcel `39009-A029050100100` ("0 West Circle Dr"), created 2026-08-12 and last retrieved 2026-10-05 00:02Z. That parcel comes from Ohio's statewide view, which has no owner field (REData `parcels/services/property_records/known_endpoints.py:278`). Production's Athens County jurisdiction row was last updated 2026-09-25, so REData dcbc9080 (0.3.4) never re-pointed it at the County Auditor's layer: that commit ships `seed_known_jurisdictions --replace-statewide` (`parcels/management/commands/seed_known_jurisdictions.py:101`), and the command has not been run there. Staging resolves the point to `A029050103001`, `own1` STATE OF OHIO. The cached polygon still contains the point (it lies 96% inside the Auditor's parcel), so the old parcel may keep answering after a re-point until it is re-resolved | not yet (REData) |
| historic_maps | Athens | `maps/` and `maps/volumes/` are empty (request `ed005e3f4f3a4013a17b65982e670c84`). Staging's `maps/volumes/` has 10, including six Athens volumes at 0 m. The atlas exists: production's own `imagery/` found six Athens Sanborn sheets, 1885-1914, through `loc_sanborn`. Production's volume catalogue (`parcels/services/historical_maps/lookup.py:246`) lacks what the targeted harvest stored on staging | REData P112 |
| footprints | HRSH | 27 of 84 on-property buildings outlined, 32% (staging: 27 of 78). The answer carried `X-REData-Unanswered-Sources: overpass`, which the suite does not read | REData P114, P103 |
| build_dates | Athens | none of 19 on-property buildings carries a year | REData P111, being fixed for 0.3.6 |
| incidents | HRSH, Athens | `count: 0`, every provider `not_applicable` | REData P110 |

Athens's buildings answer also came without Overpass: 19 on-property buildings, against 30 on staging. Its
`buildings` and `footprints` checks pass anyway. The image-search fault being fixed for REData 0.3.6 (an all-timed-out
answer read as empty, `core/services/search.py:858`) did not occur: both campuses' image searches passed.

St. Lawrence and Harlem Valley still stand at staging 0.3.5. St. Lawrence fails footprints (P114) and image search
(an empty answer, the fault being fixed for 0.3.6), and photos are inconclusive. Harlem Valley fails footprints (P114) and historic_maps (P112), and documents and news
are inconclusive under staging's share. Both fail incidents (P110). The pipeline layer (`test_pipeline.py`) was last
run on 0.3.0, on 2026-10-05, and passed for all four apart from Athens's build date (P111).

**Running the rest needs a decision.** The first question is whether a provider's `rate_limited` inside an
otherwise-answered response should end the run. REData refuses that source without calling it, so nothing billed
is spent. This run treated it as a refusal, as it would a key's `key_budget_exhausted` or a `429`. If it should keep ending the
run, production's `searxng_media` and `map_warper` budgets need headroom for live requests (REData P108). Otherwise
any campus whose media lookup is not cached can stop it. What is left: St. Lawrence, Harlem Valley, and the 53 in
six batches of eight or nine. At about 17 requests a campus, that is near 950 requests, at least two hours at under 600 an hour.

## Priorities

**P1 - blocks the goal on production.**

1. Deploy UrbanLens 0.9.0 (Jess approved it and REData 0.3.0's deploy on 2026-10-05). REData's half is done:
   releases 0.3.0 to 0.3.4 are all ancestors of its `v0.3.4` tag, which has run in production since 2026-10-06
   15:21Z, and production ran v0.3.5 for the live-locations run above. So what had been fixed only on staging (NY
   parcels by polygon, campus footprints beyond the parcel box, Athens County's owner, web and news search, the
   cultural-resource cache, Chronicling America descriptions, per-provider `limit`, the loc.gov walk) is in REData's
   production code. Two of those have not reached production's data: Athens County's jurisdiction row was never
   re-pointed (`seed_known_jurisdictions --replace-statewide`), and the loc.gov volumes for Athens were never
   harvested there (see above). UrbanLens's own half ships with 0.9.0 (`release/v_0_9_0`; production runs 0.8.0).
   The live-locations run above reached two of the four primaries on production before a refusal stopped it. The order was required, not only preferred: inside the US 0.9.0 reads Overture only
   from REData (P110), whose lookups timed out before the index-backed ones (P240); that order is now met.
2. Background media sweeps that leave a live request its share of the free SearXNG-media and Commons budgets
   (REData P108). The paid Google Places budget is not raised; UrbanLens keeps its searches few and honours
   REData's `Retry-After` (P315, REData P70).
3. Web search that does not rest on mwmbl alone (REData P113): the self-hosted SearXNG relays to engines that
   refuse the shared egress IP, so either more engines that tolerate it or one keyed index.

**P2 - data gaps on the campuses.**

4. Footprints for the CRIS roster rows no source matches, or a way to tell a demolished one (REData P114).
5. An atlas for Wingdale, or another map source for Harlem Valley (REData P112).
6. An incident source for New York and Ohio (REData P110).
7. Ohio build years (REData P111).
8. A completeness envelope on the buildings endpoints instead of a header (REData P103).
9. New York centroid parcels with no polygon, and Maryland's centroid layer (REData P101, P102).

**P3 - ingestion and outsourcing.**

10. REData endpoints no UrbanLens surface reads yet: `addresses`, `elevation/profile`, geocode autocomplete and
    structured, `related-buildings`, `land-use-areas` (P9, needs a map-overlay decision).
11. Direct third-party calls REData already answers, which could go through it (each a separate decision):
    Wikipedia geosearch and summary (REData has no infobox or full extract, so not the rest), Nominatim reverse,
    Google Geocoding outside CID resolution, Esri and USGS imagery (excluded on purpose today), and the
    OpenHistoricalMap time slider's coverage query. The Overture building-attributes panel and the boundary chain's
    Overture step read REData inside the US since P110.
    Assistant tools stay direct by design: REData is off under the AI process role.

## Known limits of what was built

- A partial answer is dated back so it lapses within the hour; raising `external_data_cache_days` afterwards
  lengthens it too (`LocationCache.set`).
- Background enrichment fills only locations with no cache row, so a lapsed partial row is refreshed by the next
  page view, not by the sweep.
- A bootstrap whose pin is deleted before its first stage leaves the location's in-flight marker for its hour, so
  enrichment skips that location until then. The task knows only the pin's id.
