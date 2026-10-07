# PL9 — Every REData answer UrbanLens can use, surfaced, cached for as long as it is true, and checked against real campuses

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL9` · `status: live` · `updated: 2026-10-06`

## What done means

- UrbanLens ingests and shows nearly every REData endpoint that has a use here, and works without REData at all.
- A REData answer is cached for as long as it is true: a partial answer briefly, an outage not at all.
- A pin dropped on a historic Kirkbride campus becomes the whole property: the parcel on the top pin and its wiki,
  a child pin and child wiki per building with its outline, build dates, owner, register listing, Wikipedia,
  archives, photos, web and news results, incidents and historic maps.
- `tests/live_locations/` checks all of that against a real REData for the four primary campuses (HRSH,
  St. Lawrence, Harlem Valley, Athens) and the rest of the 57 in `kirkbrides.toml`. See `LOCATION_DATA_TESTS.md`.

## Where it stands (2026-10-06, REData staging on v0.3.5)

`bin/run_live_location_tests.sh` against REData staging 0.3.5, 2026-10-06 23:35-23:54Z: 51 of the 64 endpoint
checks on the four primary campuses pass, 10 fail and 3 are inconclusive, against 43, 15 and 6 on 0.3.0 the day
before. Pytest reads the same run as 50 passed, 9 known issues and 5 failed. The five are Athens's web search,
which now passes against its `known_issues` entry, St. Lawrence's image search, and the three inconclusive.

**The other 53 campuses were not run.** The primaries batch already drew refusals from REData staging's own budgets
(`rate_limited` on library_of_congress, chronicling_america, gdelt, searxng, searxng_media, flickr, instagram and
wikimedia_commons), and the run stopped there, as a batch with budget refusals ends it. Waiting for the next day
would not help. Since REData 0.3.4, staging spends 0.05 of every external budget (REData `docs/BILLED_APIS.md`,
R20), which rounds the per-minute budgets of library_of_congress, chronicling_america, gdelt, internet_archive and
wikidata down to 0. Staging therefore never calls them, and it gets one call a minute for searxng, searxng_media
and wikimedia_commons, with 50, 150 and 100 a day. A campus needs several SearXNG searches and a SearXNG-media
fan-out, so on staging the 53 would take days, and documents and news would be decided only from the few sources
still open there. Running them needs a decision: an `RD_ENVIRONMENT_SHARE_OVERRIDES` window on staging for those
services, or a run against production with a key of its own. The run saw no `key_budget_exhausted` and no `429`. It
made 130 REData requests. 65 were `503`s from REData's own limiter, which the suite retried and which never left
REData. 2 were SearXNG news searches that came back empty while engines were throttled.

What still fails or is undecided:

| Check | Sites | Why | Tracked |
|---|---|---|---|
| footprints | HRSH 35% (27 of 78), St. Lawrence 61% (88 of 144), Harlem Valley 76% (66 of 87) of on-property buildings outlined | Every building left without an outline is a CRIS roster row that no footprint matches. Harlem Valley's answer also carried `X-REData-Unanswered-Sources: overpass`, which the suite does not read | REData P114, P103 |
| incidents | all four | `count: 0`, with all 148 providers `not_applicable` | REData P110 |
| build_dates | Athens | none of the 30 on-property buildings carries a year | REData P111 |
| historic_maps | Harlem Valley | five volumes, all `same_county` at 27 km (Poughkeepsie's) | REData P112 |
| web_photos | St. Lawrence | SearXNG image search answered `200` with no result for `"St. Lawrence State Hospital" Ogdensburg` (request `e723591f5ecc436fb2750ec2fec3a12e`), where the same query's web search found 10; it passed on 0.3.0. An image search whose engines failed for any reason but a block is returned as an empty answer (REData `core/services/search.py`, `_search` and `_blocked_engines`), so it may be the engines rather than the web | not yet |
| photos | St. Lawrence, inconclusive | nothing near the point, with flickr and instagram refused: the media fan-out asks SearXNG once per platform, and staging allows one a minute | staging share (REData R20) |
| documents | Harlem Valley, inconclusive | the Internet Archive's results do not name the campus, and staging never asks library_of_congress or chronicling_america | staging share (REData R20) |
| news | Harlem Valley, inconclusive | staging never asks GDELT; SearXNG news came back empty with brave.news and google news throttled | staging share (REData R20), REData P116 |

Fixed since 0.3.0: web search for Athens (10 results that name it, the Wikipedia article first), and photos, now
found for HRSH, Athens and Harlem Valley where three of the four were inconclusive on 0.3.0. Athens's `web_search`
entry in `kirkbrides.toml` now fails the run as a strict xpass and should go. The footprints, incidents,
build_dates and historic_maps rows are `known_issues` there, the same as on 0.3.0, so each turns red when fixed.
The last four rows are new since 0.3.0 and are not.

The pipeline layer (`test_pipeline.py`: one pin dropped on the campus, UrbanLens's own bootstrap run against the
same REData) was not re-run on 0.3.5. On 0.3.0 on 2026-10-05 it passed for all four primary campuses: the parcel
under the top pin and its wiki, a child pin per building with at least 80% outlined, each resolving to a building
wiki under the campus wiki, and the property records, register listings and image search cached. The one gap was
Athens's build date, which waits on REData P111.

## Priorities

**P1 - blocks the goal on production.**

1. Deploy UrbanLens 0.9.0 (Jess approved it and REData 0.3.0's deploy on 2026-10-05). REData's half is done:
   releases 0.3.0 to 0.3.4 are all ancestors of its `v0.3.4` tag, which has run in production since 2026-10-06
   15:21Z, so what had been fixed only on staging (NY parcels by polygon, campus footprints beyond the parcel
   box, Athens County's owner, web and news search, the cultural-resource cache, Chronicling America
   descriptions, per-provider `limit`, the loc.gov walk) is in REData's production code. UrbanLens's own half of those
   ships with 0.9.0 (`release/v_0_9_0`; production runs 0.8.0), and the live-locations run above was against staging,
   not re-run on production. The order was required, not only preferred: inside the US 0.9.0 reads Overture only
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
