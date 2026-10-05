# PL9 — Every REData answer UrbanLens can use, surfaced, cached for as long as it is true, and checked against real campuses

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL9` · `status: live` · `updated: 2026-10-05`

## What done means

- UrbanLens ingests and shows nearly every REData endpoint that has a use here, and works without REData at all.
- A REData answer is cached for as long as it is true: a partial answer briefly, an outage not at all.
- A pin dropped on a historic Kirkbride campus becomes the whole property: the parcel on the top pin and its wiki,
  a child pin and child wiki per building with its outline, build dates, owner, register listing, Wikipedia,
  archives, photos, web and news results, incidents and historic maps.
- `tests/live_locations/` checks all of that against a real REData for the four primary campuses (HRSH,
  St. Lawrence, Harlem Valley, Athens) and the rest of the 57 in `kirkbrides.toml`. See `LOCATION_DATA_TESTS.md`.

## Where it stands (2026-10-05, REData staging on release/0.3.0)

`bin/run_live_location_tests.sh` against REData staging after the 0.3.0 deploy: 43 of the 64 endpoint checks on
the four primary campuses pass, 15 fail and 6 are inconclusive, against 31, 29 and 4 that morning. What still
fails or is undecided:

| Check | Sites | Why | Tracked |
|---|---|---|---|
| footprints | HRSH 35%, St. Lawrence 61%, Harlem Valley 72% of on-property buildings outlined | Staging reads Overture since 2026-10-05 (`overture_ro`), and every building left without an outline is a CRIS roster row no footprint source matches: some demolished, some perhaps a CRIS point off its building | REData P114 |
| historic_maps | Harlem Valley | loc.gov holds no Sanborn atlas of Wingdale, and REData offers Poughkeepsie's, 26 km off, as same-county. HRSH, St. Lawrence and Athens pass since a paced harvest on 2026-10-05, once REData `052692a6` stopped the walk failing after its first page | REData P112 |
| incidents | all four | no incident source covers New York or Ohio | REData P110 |
| build_dates | Athens | Ohio buildings come from OSM and Microsoft, which carry no year | REData P111 |
| web_search | Athens | Only mwmbl answers staging now that yep refuses it too, and it has nothing for Athens. HRSH, St. Lawrence and Harlem Valley pass since REData stopped reading an empty answer, or one index timing out, as a block that backs all web search off (`4c883a78`, `10425d61`) | REData P113 |
| photos | three of four, inconclusive | background sweeps spend the Commons and SearXNG-media budgets by about 03:30 UTC | REData P108 |

These are `known_issues` in `kirkbrides.toml`, so each turns red when fixed. The footprints, historic_maps and web_search rows
were re-run against REData `10425d61` the same morning.

The pipeline layer (`test_pipeline.py`: one pin dropped on the campus, UrbanLens's own bootstrap run against the
same REData) passes for all four primary campuses: the parcel under the top pin and its wiki, a child pin per
building with at least 80% outlined, each resolving to a building wiki under the campus wiki, and the property
records, register listings and image search cached. The one gap is Athens's build date, which waits on REData P111.

## Priorities

**P1 - blocks the goal on production.**

1. Deploy REData 0.3.0 to production, then UrbanLens 0.9.0 (Jess approved both on 2026-10-05). Everything above
   that is fixed is fixed only on staging until then: NY parcels by polygon, campus footprints beyond the parcel
   box, Athens County's owner, web and news search, the cultural-resource cache, Chronicling America
   descriptions, per-provider `limit`, the loc.gov walk. The order is required, not only preferred: inside the US
   0.9.0 reads Overture only from REData (P110), whose lookups time out before 0.3.0 (P240).
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
