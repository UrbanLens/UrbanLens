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

## Where it stands (2026-10-07, all 57 campuses against REData production)

`bin/run_live_location_tests.sh` checked all 57 campuses against REData production (`https://redata.urbanlens.org`)
with UrbanLens production's own key, 2026-10-07 01:09-05:06Z. Production was chosen so the answers are cached for
real requests; staging's share is unchanged. REData production ran v0.3.5 until a deploy at 03:05:28Z and v0.3.6
after it. The four primaries and 23 other campuses ran on 0.3.5, the other 30 on 0.3.6, and Athens was run again on
0.3.6 at the end.

Each campus had a pytest session of its own, 60 s apart, in batches of eight or nine with 5 minutes between them,
and never more than 350 requests in the last hour. The harness never retried (`UL_LIVE_MAX_WAIT_SECONDS=0`). A
source's `rate_limited` inside an answered response was recorded and the run went on. A request-level refusal
would have stopped it, and none came: no `429`, no `key_budget_exhausted` and no `503` from REData's limiter. The
run paused five times for a person to judge, and each time it resumed:
- twice on a `503` whose causes were all upstream: GDELT's own `429` back-off, with SearXNG's news engines
  throttled;
- once on a parcel lookup's `503 no_data_found`;
- once for the 0.3.6 deploy, whose app restart answered one campus's first request with `502` (that campus was run
  again);
- once to stop the suite asking again after a `502` that had cost a gunicorn worker.

It made 956 REData requests: 933 from the suite and 23 diagnostic reads (20 parcel records, one register re-read,
`capabilities/` and `schema/`). The busiest hour held 305.

| Group | REData | Campuses | Checks | Passed | Failed | Known issue | Inconclusive | Not applicable |
|---|---|---|---|---|---|---|---|---|
| Primaries | 0.3.5 | 4 | 64 | 50 | 5 | 8 | 1 | 0 |
| Primaries, Athens re-run | 0.3.6 | 1 | 16 | 14 | 0 | 2 | 0 | 0 |
| The other 53 | 0.3.5 | 23 | 368 | 216 | 114 | 0 | 26 | 12 |
| The other 53 | 0.3.6 | 30 | 480 | 320 | 131 | 0 | 14 | 15 |
| Primaries, staging 0.3.5 (2026-10-06) | | 4 | 64 | 51 | 10 | (in failed) | 3 | 0 |
| Primaries, staging 0.3.0 (2026-10-05) | | 4 | 64 | 43 | 15 | (in failed) | 6 | 0 |

"Known issue" is a `known_issues` entry failing as expected. "Not applicable" is a demolished campus's building
checks. Harlem Valley's `historic_maps` passed against its entry, a strict xpass, and is counted as passed. Counted
the staging way, with known issues among the failures, the primaries come to 50, 13 and 1 on production 0.3.5,
against 51, 10 and 3 on staging. With Athens on 0.3.6 they come to 52, 11 and 1.

What differs on the primaries:
- Athens's `ownership` and `historic_maps` failed on 0.3.5 and pass on 0.3.6.
- Harlem Valley's `documents` failed on Smithsonian's 500.
- St. Lawrence's `photos` came back empty rather than inconclusive.
- Harlem Valley's `historic_maps` now passes.

### Every failure, by cause

Classes: **a** an UrbanLens bug; **b** a REData bug or gap REData can close; **c** a data gap no source covers;
**d** a budget, throttle or upstream artifact; **e** a wrong expectation in `kirkbrides.toml`. A `?` marks a class
not confirmed. File references are REData `v0.3.6`'s unless they name this repo. Request ids, and every REData-side
item, are in N47 (`docs/handoffs/redata-production-live-locations-2026-10-07.md`).

| Cause | Class | Checks | Campuses |
|---|---|---|---|
| Parcel lookup `404 unresearched`: no property-record source for the county (`parcels/services/property_records/orchestrator.py`) | b | parcel and everything on it | Anna (Union IL), Central State KY (Jefferson), Clinton Valley (Oakland MI), Elgin (Kane IL), St. Vincent's (St. Louis MO), Topeka (Shawnee KS), Traverse City (Grand Traverse MI) |
| Parcel lookup `503 no_data_found`: every configured tier returned nothing for the point | b | the same | Danville (Montour PA), St. Peter (Nicollet MN), Warren (Warren PA), Western State KY (Christian) |
| Parcel lookup `503 search_key_unavailable`: the vendor tier needs an address REData could not derive | b | the same | Bryce (Tuscaloosa AL), Osawatomie (Miami KS) |
| Parcel lookup `503 source_error`: `engineer.gomvo.org` unreachable | d | the same | Dayton (Montgomery OH) |
| `502`/`504`: a cold answer held a gevent worker until gunicorn killed it (REData P62, `gunicorn.conf.py:5`) | b | boundary, buildings, footprints, build_dates; documents | Augusta, Austin, St. Elizabeths, Richardson Olmsted; Anna, Spring Grove, Winnebago |
| `500`: a Smithsonian record's empty `indexedStructured.date` (`parcels/services/reference_documents/gateways.py:445`), which the registry does not isolate (`core/services/provider_registry.py:756`) | b | documents | 19: Arkansas, Austin, Central State IN/KY/VA, Cherokee, Clinton Valley, Columbus, Eastern Oregon, Eastern State WA, Fergus Falls, Harlem Valley, Mendota, Mississippi, Oregon, Dayton, Trenton, Warren, Western State KY |
| Archives that did not answer: loc.gov's per-minute budgets (`library_of_congress`, `chronicling_america`), Internet Archive `502`/`503`, Digital Commonwealth and LoC timeouts | d | documents | 13: Bryce, Clarinda, Danville, Dixmont, Harrisburg, Independence, the Institute of the Pennsylvania Hospital, Kalamazoo, St. Peter, Osawatomie, St. Vincent's, Topeka, Trans-Allegheny |
| No on-property building carries a year (REData P111's class; for Athens nothing REData can read dates them) | b | build_dates | 27: Agnews, Arkansas, Athens (known issue), Broughton, Central State IN/VA, Cherokee, Clarinda, Eastern Oregon, Eastern State WA, Fergus Falls, Greystone, Harrisburg, Independence, the Institute, Jacksonville, Kalamazoo, Kankakee, Mendocino, Mendota, Mississippi, Napa, Oregon, Patton, Terrell, Trans-Allegheny, Winnebago |
| CRIS roster rows no footprint matches (REData P114, P103) | b | footprints (known issues) | HRSH 27 of 84 outlined, St. Lawrence 88 of 144, Harlem Valley 66 of 92 |
| Owner in fields REData does not read: PA's `OWNER_LAST_NAME`/`OWNER_FIRST_NAME` (`parcels/services/property_records/known_endpoints.py:426`); Philadelphia's OPA not read | b | ownership | Harrisburg; the Institute of the Pennsylvania Hospital |
| Athens's row not re-pointed at the County Auditor, so a cached statewide parcel with no owner answered (fixed on 0.3.6 by the re-point and REData P128) | b | ownership | Athens on 0.3.5 |
| The statewide layer REData reads has no owner name (CA, ME, VA, WA) or a blank one (NJ, MD); county sources not researched | c? | ownership | Napa, Agnews, Mendocino, Augusta, Central State VA, Eastern State WA, Trenton, Greystone, Spring Grove, Sheppard Pratt |
| No National Register listing within reach, and no register provider for the state | c | register (now known issues) | Anna, Arkansas, Cherokee, Clarinda, Danville, Eastern Oregon, Elgin, Independence, Jacksonville (its NRHP 75000669 was removed 1984-04-18, so the catalogue no longer expects it), Mendocino, Mississippi, Napa, Osawatomie, Patton, Warren, Winnebago |
| No current listing within 500 m: Central State VA's only one, the Chapel (10000794), was removed 2017-02-07 after it collapsed, and Mayfield Cottage (69000236) is 616 m away; Terrell has none, and THC marker 8556 is published 542 m from the catalogue point, outside the grounds, past the radius | c | register (known issues) | Central State VA, Terrell |
| NPS places the campus's listing (86000851) 1.06 km from it | c | register (known issue) | Columbus |
| Listed under a number the catalogue lacked, because the listing is one of the campus's buildings or its district (NPS, cross-checked against Wikipedia's NRHP lists; corrected in `kirkbrides.toml`) | e | register | Central State IN (`72000011`, Old Pathology Building), Eastern State WA (`97001084`, Roosevelt Hall), Kalamazoo (`72000624`, Water Tower), Mendota (`88002183`, the Wisconsin Memorial Hospital Historic District), St. Peter (`86002117`, Center Building) |
| A historic district named only in the rows' `attributes` (New Jersey's `HD_NAME`; SHPO-eligible, with no NRHP listing): the check read only names, and now also reads a district named in an attribute, and says in its result when the match is an eligible record, not a listing (UrbanLens#340, "register check reads only each row's name") | a | register | Trenton |
| A listed NRHP property within 500 m not returned; REData's cultural-resource cache may ignore the radius | b | register | Greystone (00000653) |
| The article has no coordinates | c | wikipedia (known issues) | Central State VA, Kalamazoo |
| The article's coordinates are not marked primary, and REData's geosearch reads primary ones only | b | wikipedia | Eastern Oregon |
| Image engines refuse REData's shared egress IP (REData P116), leaving 0-7 results | d | web_photos | 19: Anna, Arkansas, Augusta, Central State KY, Cherokee, Clarinda, Danville, Eastern Oregon, Eastern State WA, the Institute, Jacksonville, Kalamazoo, Kankakee, Mississippi, Spring Grove, St. Vincent's, Trenton, Western State KY, Winnebago |
| An image search whose engines timed out answered empty (`core/services/search.py:858`, fixed in 0.3.6) | b | web_photos; photos? | St. Lawrence; St. Lawrence, Central State KY, Danville, Mississippi (0.3.5) |
| GDELT refused production's egress (five `429`s from 01:11Z), and SearXNG's news engines were throttled; a `200` from the fallback does not say GDELT was skipped | d | news | 17: Anna, Clinton Valley, Columbus, Dixmont, Fergus Falls, Harlem Valley (inconclusive), Independence, Kalamazoo, Kankakee, Northampton, Patton, Sheppard Pratt (inconclusive), Dayton, St. Vincent's, Terrell, Topeka, Trenton |
| The news query was a shared name without its town, and now carries it (UrbanLens#339, "news check searches a shared campus name without its town") | a | news | Central State KY, Western State KY |
| A media source did not answer (`wikimedia_commons` unavailable, `searxng_media` rate-limited) | d | photos (inconclusive) | Anna, Clinton Valley, Independence, Kankakee |
| Every media source answered, with nothing near the point | c? | photos | Cherokee, Patton, Winnebago |
| loc.gov holds the town's Sanborn atlas, production's catalogue does not (REData P112; `parcels/services/historical_maps/lookup.py:246`) | b | historic_maps | Central State IN (31 volumes), Jacksonville (7), Western State KY (9); Athens on 0.3.5 (passes on 0.3.6) |
| loc.gov has no Sanborn of the place | c | historic_maps (known issue) | Mississippi (Whitfield) |
| No incident provider covers the point: all 148 `not_applicable` (REData P110's class; public feeds not researched) | c? | incidents | 46 campuses, the primaries among them (their known issues) |
| The one covering feed holds nothing within 2 km | c | incidents (known issues) | Central State KY (`louisville_fire`), Central State VA (`chesterfield_va_fire_ems`) |

### What changed in the suite

- **`kirkbrides.toml`.** Twenty-two `known_issues` entries are new, all confirmed (c): 17 register, 2 wikipedia,
  1 historic_maps and 2 incidents, each with its evidence. Harlem Valley's `historic_maps` entry is gone: its
  volume answer came back `near` at 0 m (request `c664b3b4d793469b90071c593aab429f`). The (e) items are settled
  from NPS's NRHP layers, Wikipedia's NRHP lists and the state sources, each named in the entry's comment:
  - five campuses carry the number of their own building or district, and Jacksonville's removed one is no longer
    expected;
  - Central State VA and Terrell gain a register known issue (c);
  - Terrell's point stands: it is on the grounds, beside the water tower, and THC's own published point for its
    marker is the one 542 m out;
  - Mendocino stays `standing`: its Kirkbride was razed in 1952, but later hospital buildings still stand at the
    point. `status` means whether buildings stand at the catalogue point (it gates the building, footprint and
    build-date checks), not whether the original Kirkbride survives, so Central State IN, Jacksonville, Mendota,
    Kalamazoo, St. Peter, Central State VA, Terrell and Eastern State WA, which Wikipedia also records as having lost
    theirs, stay `standing` too. The catalogue's header says so.

  The nine `demolished` entries came from Wikipedia's status column, which describes the Kirkbride, so each was
  rechecked on 2026-10-07 against OSM building footprints (50 m2 or more) within 100 m of the point and an Esri aerial
  (2023-2025). Seven have buildings there and are now `standing`: Spring Grove (on the point), Danvers (on the point),
  Taunton (34 m), Northampton (25 m), Topeka (a new building on the point that OSM lacks), Columbus (79 m: two state
  office buildings, none a hospital's) and Central State KY (88 m: one park building; the 1996-demolished campus is
  now a state park). The last two are borderline. Dixmont (nothing within 200 m) and Clinton Valley (open field; the
  nearest houses are 136 m away, also borderline) stay `demolished`. The seven's building checks now run, and none
  has been run against REData. The 47 `standing` entries not mentioned here were not rechecked.
- **Five harness-only fixes:**
  - the URL, key and Host are read from `.env` together or not at all;
  - a `503` carrying a REData error code is remembered for the session;
  - a call still refused when its wait ran out is not asked again by each later check;
  - the news search carries the town and state after a name that needs one, and never searches a Wikipedia
    disambiguator (UrbanLens#339);
  - the register check counts a row naming the campus as a historic district in its `attributes`, or carrying its
    NRHP number there, and never counts a record the register withdrew (not eligible, delisted, removed). When every
    match is a SHPO's eligible record, which is not a listing, its result says so (UrbanLens#340).

  The third cut repeated worker kills on production to one per endpoint per campus.
- **UrbanLens#339 and UrbanLens#340** hold the two suite gaps. **N47** is the handoff to REData.

Not run on production: the pipeline layer (`test_pipeline.py`). It last ran on staging 0.3.0, on 2026-10-05, and
passed for all four primaries apart from Athens's build date (REData P111).

## Priorities

**P1 - blocks the goal on production.**

1. Deploy UrbanLens 0.9.0 (Jess approved it and REData 0.3.0's deploy on 2026-10-05). REData's half is done:
   production runs v0.3.9 (the coordinating session's audit of 2026-10-07; not re-checked live for this edit), which carries v0.3.7 (in production from 2026-10-07 08:30Z) and v0.3.6 (from 03:05Z), so what had been fixed only on staging (NY parcels by polygon,
   campus footprints beyond the parcel box, Athens County's owner, web and news search, the cultural-resource cache,
   Chronicling America descriptions, per-provider `limit`, the loc.gov walk) is in REData's production code, and
   Athens's owner and atlas are in its data. UrbanLens's own half ships with 0.9.0 (`release/v_0_9_0`; production
   runs 0.8.0). The order was required, not only preferred: inside the US 0.9.0 reads Overture only from REData
   (P110), whose lookups timed out before the index-backed ones (UrbanLens#292, "Overture needs REData's
   index-backed lookups"); that order is now met.
2. REData's fixes from N47 that every UrbanLens user meets: the Smithsonian parse error that fails archive search
   for 19 of the 57 campuses, and cold buildings and boundaries answers that cost a gunicorn worker (REData P62).
   Both are answered by REData 0.3.7 (tag `v0.3.7`, `3a2b017d`), in production since 2026-10-07 08:30Z: the Smithsonian error (N47 item 1) is isolated as one
   `unavailable` archive, and a cold parcel's buildings and boundaries answer 503 `refresh_queued` while REData
   computes them (REData #147). REData's P62 stays open after #147 (0.3.7) and #153 (0.3.8, CRIS site reads and footprint
   placement): a filtered `?source=` call has no deadline, and some serializers are unaudited. UrbanLens's half of both is in
   UrbanLens#352 (`fix/redata-partial-answers`), merged into `release/v_0_9_0`, so 0.9.0 reads them and production's 0.8.0 does not; see "How a partial REData answer is cached" below. Its vendored
   schema is from `release/0.3.7` `c4e0de94`; the tag adds only release-please's version bump and one REData test (#150).
3. Background media sweeps that leave a live request its share of the free SearXNG-media and Commons budgets
   (REData P108, fixed by `4810a6c5` in v0.3.4; not re-measured). The paid Google Places budget is not raised; UrbanLens keeps its searches few and honours
   REData's `Retry-After` (P315, REData P70).
4. Web search that does not rest on mwmbl alone (REData P113): the self-hosted SearXNG relays to engines that
   refuse the shared egress IP, so either more engines that tolerate it or one keyed index.

**P2 - data gaps on the campuses.**

5. Footprints for the CRIS roster rows no source matches, or a way to tell a demolished one (REData P114).
6. Parcels for the 14 campuses whose county REData cannot answer, and owners where its layer has none (N47 §4).
7. Production's Sanborn catalogue for the towns loc.gov holds atlases of (REData P112, N47 §3).
8. An incident source beyond the eight cities that have one (REData P110: 46 of the 57 campuses have none).
9. Build years outside New York (REData P111: 27 campuses).
10. A completeness envelope on the buildings endpoints instead of a header (REData P103).
11. New York centroid parcels with no polygon, and Maryland's centroid layer (REData P101, P102).
12. A decision on whether every catalogue campus should be expected to have incidents and news. `status` is decided
    (buildings stand at the catalogue point, not whether the Kirkbride survives) and applied to all nine `demolished`
    entries; the first live run will show whether Columbus and Central State KY, whose only buildings are not
    hospital ones, belong with the `standing` campuses.

**P3 - ingestion and outsourcing.**

13. REData endpoints no UrbanLens surface reads yet: `addresses`, `elevation/profile`, geocode autocomplete and
    structured, `related-buildings`, `land-use-areas` (UrbanLens#267, "land-use-area boundary geometry is not drawn").
14. Direct third-party calls REData already answers, which could go through it (each a separate decision):
    Wikipedia geosearch and summary (REData has no infobox or full extract, so not the rest), Nominatim reverse,
    Google Geocoding outside CID resolution, Esri and USGS imagery (excluded on purpose today), and the
    OpenHistoricalMap time slider's coverage query. The Overture building-attributes panel and the boundary chain's
    Overture step read REData inside the US since P110.
    Assistant tools stay direct by design: REData is off under the AI process role.

## How a partial REData answer is cached

A REData answer that says a source did not answer is shown with whatever did come back, kept briefly, and asked for
again. It is never recorded as "nothing there". REData says so in one of four ways: `complete: false` with a
`providers` block naming the source as `unavailable`, `rate_limited`, `key_budget_exhausted` or `not_cached`; the
parcel record's own unanswered tiers; the `X-REData-Unanswered-Sources` header on the buildings answers; or, from
0.3.7, a 503 `refresh_queued` or `compute_timeout` for a parcel it is still computing. REData 0.3.7 to 0.3.9 send
that code in `error`. From 0.3.10 `error` is `source_error` and the code is in `pending`, so that UrbanLens 0.8.0,
which knows neither code, retries rather than caching the 503 as "no buildings". A REData that sends none of these
(0.3.6 and older on most endpoints) is read as complete, exactly as before.

| What UrbanLens keeps | Where | When REData answered in part |
| --- | --- | --- |
| Panel rows (`LocationCache`): info panels, news, archives, hazard history, elevation, site conditions, parcel buildings, property records | `LocationCache.set` | the payload names its `unanswered_sources`, so the row lapses after `PARTIAL_ANSWER_STALE_AFTER` (1 hour) instead of `external_data_cache_days` |
| A partial answer with nothing in it | `run_panel_fetch` | not cached; the panel is skipped for `FAILURE_SKIP_TTL_SECONDS` (5 minutes), or for REData's own wait when it names one |
| Satellite and street-view carousels | Django cache | shown but not cached, and warmed again after 5 minutes; only a complete set is kept for the window. A slide whose image REData could not serve for now (a 5xx, a throttle, no answer) makes the set partial too; a 4xx such as no image for the asset does not |
| Building lists that fall back to OSM and CRIS when REData or a fallback was unreachable | `fetch_parcel_buildings` | the list names what was missing, so it lapses within the hour |
| A parcel REData is still computing (buildings, boundaries) | gateway, panel, bootstrap, boundary chain | nothing cached and no fallback stands in; the panel waits the body's `retry_after`, the bootstrap asks the same stage again after it (waits of up to 5 minutes, at most 5 times; a throttle or outage moves on as before), and the boundary chain retries after it with no back-off |
| A parcel's boundary from a partial `/boundaries/` or building list | `RedataBoundaryProvider` | deferred, not drawn: neither REData's suggestion from a partial candidate set nor the hull of part of the buildings is kept as the parcel's line; a deferred boundary is retried, and never recorded as a miss |
| Property record sections (assessments, sale records, liens, tax, owners, sales, demographics, national parks) | `_fetch_payload` | a section REData could not answer for now (a 5xx, a throttle, no answer) is named, so the record lapses within the hour; a refusal REData gives for good is not: a 404, a 403 or the breaker holding one, or no Census key configured |
| Trivia, build dates, official owners | derived from the rows above | a partial building list withdraws only the year-built questions about buildings it names, asks no building-count question and retracts no build year; a partial parcel record links the owner it names and unlinks nobody |

The live-locations harness waits out a `refresh_queued` or `compute_timeout` once, asks once more, and reports the
parcel inconclusive if it is still computing.

## Known limits of what was built

- A partial answer is dated back so it lapses within the hour; raising `external_data_cache_days` afterwards
  lengthens it too (`LocationCache.set`).
- Background enrichment fills only locations with no cache row, so a lapsed partial row is refreshed by the next
  page view, not by the sweep.
- `/search/web/` sends no `complete`, so a web search missing an engine cannot be told from a complete one.
- One near-point answer is shared for `SHARE_SECONDS` (10 minutes) across a page's panels whether or not it is
  complete; each panel's own row then lapses within the hour.
- A bootstrap whose pin is deleted before its first stage leaves the location's in-flight marker for its hour, so
  enrichment skips that location until then. The task knows only the pin's id.
