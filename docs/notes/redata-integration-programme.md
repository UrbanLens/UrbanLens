# PL9 — Every REData answer UrbanLens can use, surfaced, cached for as long as it is true, and checked against real campuses

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL9` · `status: live` · `updated: 2026-10-08`

## What done means

- UrbanLens ingests and shows nearly every REData endpoint that has a use here, and works without REData at all.
- A REData answer is cached for as long as it is true: a partial answer briefly, an outage not at all.
- A pin dropped on a historic Kirkbride campus becomes the whole property: the parcel on the top pin and its wiki,
  a child pin and child wiki per building with its outline, build dates, owner, register listing, Wikipedia,
  archives, photos, web and news results, incidents and historic maps.
- `tests/live_locations/` checks all of that against a real REData for the four primary campuses (HRSH,
  St. Lawrence, Harlem Valley, Athens) and the rest of the 57 in `kirkbrides.toml`. See `LOCATION_DATA_TESTS.md`.

## Where it stands (2026-10-08, all 57 campuses against REData production 0.3.10)

`bin/run_live_location_tests.sh` checked all 57 campuses against REData production (`https://redata.urbanlens.org`,
v0.3.10) with UrbanLens production's own key, 2026-10-07 23:36Z to 2026-10-08 02:26Z. The suite is `release/v_0_9_0`
at `f77477412` plus one catalogue line (Greystone's `nrhp`, below). Each campus had a pytest session of its own, 60 s
apart, in batches of nine with 5 minutes between, never more than 350 requests in the last hour (the busiest held
328), and `UL_LIVE_MAX_WAIT_SECONDS=0`: every call was asked once and none re-asked, so a `503 refresh_queued` counts
as inconclusive and is not waited out. It made 881 suite requests and about 50 diagnostic reads to explain failures. The
statuses were 869 `200`, 9 `503`, 2 `504` and one `404`: no `429`, no `key_budget_exhausted`, no `500`, no `502`.
Production's app log held no `WORKER TIMEOUT` and no `SIGKILL` from 22:30Z to 02:30Z, against 7 to 16 an hour in the
last run. No hosted-AI call was made, and staging's share was not touched.

| Group | REData | Campuses | Checks | Passed | Failed | Known issue | Inconclusive | Not applicable |
|---|---|---|---|---|---|---|---|---|
| Primaries | 0.3.10 | 4 | 64 | 52 | 2 | 8 | 2 | 0 |
| The other 53 | 0.3.10 | 53 | 848 | 617 | 161 | 23 | 41 | 6 |
| All 57 | 0.3.10 | 57 | 912 | 669 | 163 | 31 | 43 | 6 |
| Primaries, previous run (Athens on 0.3.6) | 0.3.5, 0.3.6 | 4 | 64 | 52 | 11 with known issues | (in failed) | 1 | 0 |
| The other 53, previous run | 0.3.5, 0.3.6 | 53 | 848 | 536 | 245 | 0 | 40 | 27 |
| Primaries, staging 0.3.5 (2026-10-06) | | 4 | 64 | 51 | 10 | (in failed) | 3 | 0 |
| Primaries, staging 0.3.0 (2026-10-05) | | 4 | 64 | 43 | 15 | (in failed) | 6 | 0 |

"Known issue" is a `known_issues` entry failing as expected. "Not applicable" is a demolished campus's building
checks: six now (Clinton Valley and Dixmont), against 27 before the seven campuses that still have buildings were
made `standing`. Counted the staging way, the primaries come to 52, 10 and 2. The other 53 gained 81 passes, and their failures fell from 245 to 161. Mississippi's `historic_maps` known issue came back as a strict xpass and is counted as passed; it is
not a fix (UrbanLens#373, below).

The primaries' failures are St. Lawrence's `web_photos` and `photos` (nothing near the point) and two inconclusive
checks at Harlem Valley (`documents`, with loc.gov and Chronicling America not answering, and `news`, with the news
search unavailable). Athens's `ownership` and `historic_maps` pass, as they did on 0.3.6.

### What REData's releases fixed, seen live

| N47 item | Release | Seen on 0.3.10 |
|---|---|---|
| 1. Smithsonian `500` fails archive search | 0.3.7 (REData #145) | **Fixed.** No `500` in 881 calls; `documents` passed at 44 of 57 campuses, against 25 or fewer. The other 13 are in the table below. |
| 2. Cold boundaries and buildings hold a gunicorn worker (P62) | 0.3.7 (#147), 0.3.8 (#153) | **Fixed for 6 of 7, and no worker killed.** Augusta, Austin, St. Elizabeths, Spring Grove, Anna and Winnebago answer. Richardson Olmsted's `boundaries/` and `buildings/` answered `503 refresh_queued` (the designed answer, not re-asked). P62 stays open at REData. |
| 3. Sanborn volumes missing (P112) | 0.3.6, targeted harvest | **Fixed.** Athens, Indianapolis, Jacksonville and Hopkinsville answer `near` volumes at 0 m (10, 10, 5 and 7), and `historic_maps` passes. |
| 4. Counties with no parcel source | 0.3.7 (#143) and the seeded counties | **Mostly fixed.** Jefferson KY, Oakland MI, Kane IL, St. Louis MO, Shawnee KS and Grand Traverse MI answer a parcel (they were `404 unresearched`), and so do Montour PA, Warren PA and Nicollet MN (were `503 no_data_found`). Parcel failures fell from 14 campuses to 5. |
| 4. Pennsylvania's and Philadelphia's owner | 0.3.7 (#143) | **Philadelphia fixed, Pennsylvania not seen.** The Institute's `ownership` passes. Harrisburg's parcel `62-026-004` still has no row in `owners/` or `assessments/`, and Danville's and Warren's new parcels have none either. |
| 5. Greystone's NRHP listing at 500 m | 0.3.7 (#145) | **Fixed in REData.** `cultural-resources/lookup/` at 500 m now returns NRHP 00000653 from `nps_nrhp` and `nj_shpo`. The check still failed because the catalogue had no number and the listing's name does not say Greystone; `kirkbrides.toml` now carries `nrhp = "00000653"` (replayed on the two recorded rows, not re-asked live). |
| 6. News hides a skipped GDELT | 0.3.7 (#145) | **Fixed for the harness.** 18 campuses are now inconclusive because `complete: false` says GDELT did not answer, where 17 failed or were inconclusive before; 3 more are inconclusive on `search_unavailable`. |
| 7. Wikipedia misses a non-primary coordinate | 0.3.7 (#145) | **Not seen.** Eastern Oregon's article sits at the point (45.6715, -118.8171, no `primary` flag), and `reference-documents/?provider=wikipedia` at 1 km returned three other articles with distances. |
| 8, 9. Build years, incidents | not answered | **Unchanged.** |

### Every remaining failure, by cause

Classes: **a** an UrbanLens bug; **b** a REData bug or gap REData can close; **c** a data gap no source covers;
**d** a budget, throttle or upstream artifact; **e** a wrong expectation in `kirkbrides.toml`. A `?` marks a class
not confirmed. REData-side items are in N47 (`docs/handoffs/redata-production-live-locations-2026-10-07.md`) and
REData's T15 reply.

| Cause | Class | Checks | Campuses |
|---|---|---|---|
| Parcel lookup `404 unresearched`: no property-record source for Union County IL | b | parcel and everything on it | Anna |
| Parcel lookup `503 no_data_found` for Christian County KY (Tier 2) | b | the same | Western State KY |
| Parcel lookup `503 search_key_unavailable`: the vendor tier needs an address REData could not derive | b | the same | Bryce (Tuscaloosa AL), Osawatomie (Miami KS) |
| Parcel lookup `503 source_error`: `engineer.gomvo.org` | d | the same, inconclusive | Dayton (Montgomery OH) |
| `503 refresh_queued` on `boundaries/` and `buildings/` (REData P62's designed answer), not re-asked | b? | boundary, buildings, footprints, build_dates, inconclusive | Richardson Olmsted |
| `reference-documents/search/` answered `504` at 90 s; no worker was killed | b | documents, inconclusive | Cherokee, Traverse City (REData P62's class) |
| **New.** The parcel resolves in a newly seeded county and `buildings/` answers `200` with an empty list (Topeka's parcel is 2700 SW 3rd St, 277,250 sq ft; Traverse City's is 1001 W Eleventh St) | b? | buildings, footprints | Topeka, Traverse City |
| No on-property building carries a year (REData P111's class; for Athens nothing REData can read dates them) | b | build_dates | 39 campuses, and Athens as a known issue; Anna, Bryce, Osawatomie and Western State KY never got a parcel: Agnews, Arkansas, Athens, Augusta, Austin, Broughton, Central State IN/KY/VA, Cherokee, Clarinda, Columbus, Danville, Eastern Oregon, Eastern State WA, Elgin, Fergus Falls, Greystone, Harrisburg, Independence, the Institute, Jacksonville, Kalamazoo, Kankakee, Mendocino, Mendota, St. Peter, Mississippi, Napa, Northampton, Oregon, Patton, St. Elizabeths, Taunton, Terrell, Topeka, Trans-Allegheny, Traverse City, Warren, Winnebago |
| A building count under 0.8 outlined: CRIS roster rows no footprint matches (REData P114, P103) | b | footprints | HRSH 27 of 84, St. Lawrence 88 of 144, Harlem Valley 66 of 92 (known issues); Northampton and St. Vincent's, 2 of 3 each |
| Owner in fields REData does not read, or a statewide layer with no owner name (CA, ME, VA, WA, NJ, MD), or a new county parcel whose owner is blank; county sources not researched | b / c? | ownership | Agnews, Augusta, Central State KY (new), Central State VA, Clinton Valley (new), Danville (new), Eastern State WA, Greystone, Harrisburg, Mendocino, Napa, Sheppard Pratt, Spring Grove, Trenton, Warren (new) |
| No National Register listing within reach, and no register provider for the state | c | register (known issues) | Anna, Arkansas, Central State VA, Cherokee, Clarinda, Columbus, Danville, Eastern Oregon, Elgin, Independence, Jacksonville, Mendocino, Mississippi, Napa, Osawatomie, Patton, Terrell, Warren, Winnebago |
| The article has no coordinates | c | wikipedia (known issues) | Central State VA, Kalamazoo |
| The article's coordinates are not marked primary and REData still does not return it (N47 section 7, REData T15 section 7) | b | wikipedia | Eastern Oregon |
| Image engines refuse REData's shared egress IP (REData P116), leaving 0-7 results | d | web_photos | 22: Anna, Arkansas, Augusta, Austin, Central State KY, Cherokee, Clarinda, Danville, Eastern Oregon, Eastern State WA, the Institute, Jacksonville, Kalamazoo, Kankakee, St. Peter, Mississippi, Spring Grove, St. Lawrence, St. Vincent's, Trenton, Western State KY, Winnebago |
| Every media source answered with nothing near the point | c? | photos | Anna, Central State KY, Cherokee, Clinton Valley, Danville, Independence, Mississippi, Patton, St. Lawrence, Winnebago |
| GDELT did not answer (`complete: false`), or the news search answered `503 search_unavailable` (Harlem Valley, Sheppard Pratt, St. Vincent's) | d | news, inconclusive | 21: Agnews, Anna, Central State IN, Clinton Valley, Columbus, Dixmont, Eastern State WA, Fergus Falls, Harlem Valley, Independence, Kalamazoo, Kankakee, Northampton, Patton, Sheppard Pratt, Dayton, St. Vincent's, Terrell, Topeka, Trenton, Western State KY |
| loc.gov's `library_of_congress` and `chronicling_america` did not answer | d | documents, inconclusive | 10: Anna, Austin, Clarinda, Eastern State WA, Harlem Valley, Harrisburg, the Institute, Dayton, St. Vincent's, Western State KY |
| News answered with 0 results and every source ok | c? | news | Central State KY |
| 76 archive results, none naming the campus | c? | documents | Independence |
| `maps/` returned 23 state-scale sheets, so the known issue (no Sanborn of Whitfield) became a strict xpass (UrbanLens#373) | a | historic_maps | Mississippi |
| No incident provider covers the point (REData P110's class; public feeds not researched) | c? | incidents | 46 campuses, the primaries among them |
| The one covering feed holds nothing within 2 km | c | incidents (known issues) | Central State KY (`louisville_fire`), Central State VA (`chesterfield_va_fire_ems`) |

The seven campuses made `standing` on 2026-10-07 ran their building checks for the first time. Spring Grove, Danvers,
Taunton, Northampton, Columbus and Central State KY have buildings on the property (`buildings` passes), and all but
Danvers and Spring Grove fail `build_dates` (P111's class). Topeka has none (above). Dixmont and Clinton Valley stay
`demolished`.

New failures: the empty `buildings/` for Topeka and Traverse City, Mississippi's xpass (UrbanLens#373), and the
ownership of the four counties that now have a parcel and no owner (Central State KY, Clinton Valley, Danville,
Warren). No new UrbanLens defect beyond #373.

Not run: a second ask for Richardson Olmsted's `refresh_queued`, and the pipeline layer (`test_pipeline.py`), which
last ran on staging 0.3.0 on 2026-10-05 and passed for all four primaries apart from Athens's build date.

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
  nearest houses are 136 m away, also borderline) stay `demolished`. The seven's building checks ran on
  2026-10-08 ("Where it stands"): Columbus and Central State KY have buildings on the property. The 47 `standing` entries not mentioned here were not rechecked.
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
   production runs v0.3.10 (seen 2026-10-08 in the app container's `pyproject.toml`), which carries v0.3.7 (in production from 2026-10-07 08:30Z) and v0.3.6 (from 03:05Z), so what had been fixed only on staging (NY parcels by polygon,
   campus footprints beyond the parcel box, Athens County's owner, web and news search, the cultural-resource cache,
   Chronicling America descriptions, per-provider `limit`, the loc.gov walk) is in REData's production code, and
   Athens's owner and atlas are in its data. UrbanLens's own half ships with 0.9.0 (`release/v_0_9_0`; production
   runs 0.8.0). The order was required, not only preferred: inside the US 0.9.0 reads Overture only from REData
   (P110), whose lookups timed out before the index-backed ones (UrbanLens#292, "Overture needs REData's
   index-backed lookups"); that order is now met.
2. REData's fixes from N47 that every UrbanLens user meets: the Smithsonian parse error that fails archive search
   for 19 of the 57 campuses, and cold buildings and boundaries answers that cost a gunicorn worker (REData P62).
   Both are answered by REData 0.3.7 (tag `v0.3.7`, `3a2b017d`), in production since 2026-10-07 08:30Z, and the 2026-10-08 run saw them work (no `500`, no `WORKER TIMEOUT`): the Smithsonian error (N47 item 1) is isolated as one
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
6. Parcels for the 5 campuses whose county REData still cannot answer (Anna, Bryce, Osawatomie, Western State KY, and Dayton's source down), buildings for Topeka's and Traverse City's new parcels, and owners where a layer has none (N47 §4).
7. Sanborn volumes for further towns loc.gov holds atlases of (REData P112). The four campus towns are in production's catalogue.
8. An incident source beyond the eight cities that have one (REData P110: 46 of the 57 campuses have none).
9. Build years outside New York (REData P111: 39 campuses on 2026-10-08, 12 of them newly counted because their parcels now resolve or their building checks newly run).
10. A completeness envelope on the buildings endpoints instead of a header (REData P103).
11. New York centroid parcels with no polygon, and Maryland's centroid layer (REData P101, P102).
12. A decision on whether every catalogue campus should be expected to have incidents and news. `status` is decided
    (buildings stand at the catalogue point, not whether the Kirkbride survives) and applied to all nine `demolished`
    entries. The 2026-10-08 run found buildings at Columbus and Central State KY (neither is a hospital's), so they stay
    `standing`; whether the incidents and news checks belong on every campus is still open.

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
