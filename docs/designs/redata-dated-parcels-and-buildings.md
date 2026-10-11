# PL11 — UrbanLens will read parcels and buildings as separate dated facts from REData, ask for any date, and drop cached answers when REData says they changed

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Status: live · Written 2026-10-10 against `release/v_0_9_0` at `f21fba0c5`, and rewritten the same day
for Jess's rulings (below). Nothing here is built in UrbanLens. REData's design is its PL14
(`docs/temporal-parcels-and-buildings.md` in REData); its Phase 0, which only keeps evidence, is in
REData PRs #189-#193, merged 2026-10-11, and
reaches production with REData's next release. This file describes only the REData API contract UrbanLens will
consume, and what UrbanLens does with it.

## Why

What UrbanLens reads today cannot tell these apart:

| Case | What UrbanLens sees |
|---|---|
| A vacant lot | An empty `/parcels/{uuid}/buildings/` list, cached as "no buildings" |
| A lot whose buildings no map source has drawn yet | The same empty list |
| A lot whose buildings were demolished | The same empty list, or worse, an old building still listed |
| A campus later split into a subdivision | Today's parcel only. The parcel the campus stood on is not visible. |

Other limits:

- UrbanLens never sends a date to a parcel or building endpoint (`redata_gateway.py`). The time slider
  filters historical features client-side (`services/locations/temporal_imagery.py`).
- Cached REData answers live in `LocationCache` for `external_data_cache_days`, 7 by default. Nothing
  drops them when REData's data changes.
- Child pins built from a building list are added and never removed.
- The live catalogue (`tests/live_locations/kirkbrides.toml`) has one `status` per site:
  `standing` or `demolished`.

## The contract UrbanLens will rely on

Every item is additive. Existing fields, status codes and defaults keep their meaning, so 0.8 and 0.9
are unaffected. UrbanLens uses each item only when REData's `GET /capabilities/` lists it in its
`temporal` block.

| Item | Meaning |
|---|---|
| `as_of` (ISO date, or a bare year) | Accepted on `/parcels/lookup/`, `/parcels/{uuid}/`, `/parcels/{uuid}/buildings/`, `/related-buildings/`, `/boundaries/` and the history endpoints. Absent means now. Any other endpoint answers `400 as_of_unsupported`. A date before REData's first evidence about a parcel or building answers `unknown`. |
| `known_at` (ISO date-time) | Wherever `as_of` is accepted: the answer as REData's reconciled state stood at that moment. Absent means now. For debugging a stale UrbanLens cache: "what did REData say then". |
| `include` | `demolished`, `inferred`, or both, comma-separated. Without `inferred`, REData never answers from an inference. With it, each inferred value carries `basis: inferred` and the rule behind it. |
| Parcel `building_presence` | One of `has_buildings`, `none_standing` (had buildings, all demolished), `never_built` (positive evidence of vacancy), `none_found` (every source answered, none found one, no vacancy evidence) or `unknown`. |
| Parcel `lifecycle`, `valid_from`, `valid_to`, `lineage` | Whether the parcel is active or retired, and its parents and children through splits, merges and renumbering, each with an effective date. |
| Building `building_uuid` | REData's stable building id. It survives a source being added to or leaving the building. `ref` stays served unchanged. |
| Building `status` at `as_of` | One of `standing`, `demolished`, `not_yet_built`, `uncertain` or `unknown`. Carried by every building in every answer, the default one included. |
| Building `existence` | `{start, end}`. Each bound is `{not_before, not_after, basis, sources}`, or `end: null` when nothing says the building ended. Undated sources give wide bounds rather than none. |
| `?include=demolished` | A default list leaves out buildings `demolished` at `as_of`, so it never shows a gone building as standing. This one parameter adds them to the same list, in the same request. |
| Building `conflicts` | Sources that disagree, such as a register saying demolished while a footprint source still shows the building. |
| `revision` | Bumped whenever REData's reconciled answer for that parcel or building changes. |
| `GET /parcels/{uuid}/history/`, `GET /buildings/{uuid}/`, `GET /buildings/{uuid}/history/` | Timelines of a parcel's or a building's states. |
| `GET /buildings/resolve/?ref=` | A source ref → its `building_uuid`. Never a legacy Overture hash: by then none remains (below). |
| `GET /changes/?since=<cursor>` | Parcels and buildings whose answer changed, in order. REData keeps the whole feed indefinitely, so every cursor UrbanLens was given stays valid, however long it was away. |

Served once REData's Phase 0 is released, and safe to ignore: each `sources[]` entry of a building
gains `source_as_of` (`{not_before, not_after, basis}` or `null`: the source's own date for that record),
and an Overture source's `attributes` gain `gers_id`. No existing field changes value.

**Overture refs (REData `P98`, Jess 2026-10-11).** An Overture `ref` is a content hash that changes
between Overture releases, and it has no lasting value. While UrbanLens 0.8 is in production REData keeps
serving it, plus two transitional items:

- `stable_ref` on every building: the `ref` it will have once Overture refs become
  `overture:<gers_id>`, and equal to `ref` for any other building;
- `GET /buildings/resolve/?ref=`, which maps a hash REData served since 2026-10-11 to its
  `overture:<gers_id>` (`resolved`, `ambiguous` or `unknown`).

UrbanLens 0.9 keys building places, floorplans and swept-building records on `stable_ref`, else `ref`
(`services/places/overture_refs.py`). It re-keys a place still on a hash rather than creating a second
one, and `rekey_overture_places` moves the existing ones, matching a hash REData never recorded by
footprint. A separate command, not yet built, will merge the duplicate places the drift already made. After
0.9 is in production, REData switches `ref` itself and removes the hash, `stable_ref`, the aliases and
the resolver; P98 lists the steps and their zero-references checks.

## What UrbanLens does with it

1. **Identity.** Key building places, child pins and floorplan references on `building_uuid`. Map
   existing `provider_key` values once, through `/buildings/resolve/`. Every Overture `provider_key` is
   `overture:<gers_id>` by then (above).
2. **Presence.** Show "no building on this lot", "buildings demolished", "not yet mapped" or
   "unknown" from `building_presence`. Stop falling back to OSM and CRIS lists when presence is
   settled.
3. **Status.**
   - Read `status` on every building, and pass `include=demolished` wherever the page shows what used to stand.
   - Show demolished buildings with their `existence.end` bounds ("demolished 2005-2006").
   - Fill `Pin.date_built` from `existence.start` only when its basis dates the building itself, as `own_build_year` does with `year_built_basis` today.
4. **Time.** The time slider passes `as_of` to the parcel and building endpoints, and `year` to
   `/historical-features/`, instead of filtering on the client. It shows `unknown` as unknown, never as
   "no building". It does not ask for `include=inferred` unless a later design says so.
5. **Invalidation.**
   - Keep `revision` in each `LocationCache` payload.
   - Poll `/changes/` on the hourly enrichment cycle, and drop or refresh every row whose parcel or building changed.
   - Reconcile child pins against the new list: add, retire or mark demolished. Never only add.
   - Polling is the design for now. REData may add push later; nothing here should depend on polling
     being the only path, and a push would still leave `/changes/` to fill any gap.
6. **Corrections and user knowledge stay in UrbanLens.** A user's dated photo or demolition report, and
   any correction to a building's status or dates, are UrbanLens data. They override REData's answer
   inside UrbanLens and are never sent to REData, which holds official data from external sources only
   and has no manual corrections.

## The live catalogue

Each site keeps `built` and `status`. `status` becomes derived. Three new sections each state a
different fact:

```toml
[site.parcel]                       # the parcel at lat/lng, today
presence = ["none_standing", "never_built", "none_found"]

[site.main_building]
name = "Dixmont State Hospital"
built = 1862
demolished = { not_before = 2005, not_after = 2006 }   # cite the source in a comment

[[site.as_of]]
year = 1950
main_building = "standing"
```

| New check | Asserts |
|---|---|
| `parcel_presence` | `building_presence` is one of the accepted values |
| `main_building` | The named building's `status` today, with `existence` bounds containing `built` and `demolished` |
| `as_of` | Each dated expectation against `?as_of=<year>&include=demolished`. `unknown` is inconclusive. |

A standing site sets `demolished = false` and `presence = ["has_buildings"]`. A subdivided campus
(Clinton Valley) adds `lineage = "split"` under `[site.parcel]`. Every new check skips until
`/capabilities/` lists its feature. `known_issues` works as it does today.

## Jess's rulings (2026-10-10)

- Expose `known_at` now.
- A date before the first evidence answers `unknown`. Inferences are given only on request.
- Demolished buildings are left out by default; one parameter includes them in the same request; every
  building always carries its `status`.
- REData keeps full history for all parcels, not only served ones, and keeps `/changes/` indefinitely.
- UrbanLens polls `/changes/` for now. Push may come later and is not precluded.
- UrbanLens user reports never become a REData source.
- REData has no manual corrections. Corrections live in UrbanLens.
