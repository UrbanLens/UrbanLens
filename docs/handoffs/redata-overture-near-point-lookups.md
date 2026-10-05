# REData's Overture near-point lookups time out, and UrbanLens now reads Overture from them inside the US

- **Status: ANSWERED 2026-10-05: fixed on REData `release/0.3.0` (`7ac19bf6`, `bfb47503`). Not seen live: REData staging has no Overture credentials (its P106). Production REData is still 5aabe887. UrbanLens's side (P110) ported onto `release/v_0_9_0` 2026-10-05.** Written for UrbanLens P110 (`docs/archive/PROBLEMS-ARCHIVE.md`). Probed once
  each against `https://redata.urbanlens.org` with the development key on 2026-10-03; REData read from `main`
  (`99659fcc`).
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What UrbanLens now does

Inside `is_usa_coordinates` (the same boxes REData's `overture` providers gate on), UrbanLens takes Overture only
from REData and never reads Overture's public release, even when REData answers empty:

| UrbanLens caller | REData request |
|---|---|
| boundary chain's building step, Building Characteristics panel | `GET /buildings/?lat&lng&provider=overture&radius_meters=10` |
| Building Characteristics panel's nearby places | `GET /points-of-interest/lookup/?lat&lng&provider=overture&radius_meters=150` |

It reads `BuildingRecord.attributes` as the Overture row's properties (`subtype`, `class`, `height`, `num_floors`,
`names`, and any column REData stores there), and a place's `name`, `category`, `latitude`/`longitude` and
`attributes.confidence`. A 503 is an outage: nothing is cached and the call is retried later. A 200 with no rows is
final.

## 1. Both lookups time out on the deployed REData

| request (US Capitol, 38.8895, -77.0075) | result |
|---|---|
| `GET /capabilities/?lat&lng` | 200 in 0.3 s; `buildings` and `points_of_interest` both list `overture` |
| `GET /buildings/?provider=overture&radius_meters=10` | no response within 60 s (client timeout) |
| `GET /points-of-interest/lookup/?provider=overture&radius_meters=150` | 504 from openresty at 90 s |

One probe each; not repeated, so as not to add load. `/buildings/` did not answer 403, so the development key holds
`buildings:read`.

The likely cause, from reading `main` rather than an `EXPLAIN`: `overture_buildings.lookup.find_buildings_near` and
`overture_places.lookup.find_places_near` filter with `geometry__distance_lte=(point, D(m=...))` on SRID 4326
geometry columns. For a geodetic geometry field Django compiles that to `ST_DistanceSphere(geometry, point) <= r`
(`django/contrib/gis/db/models/functions.py`, `Distance.as_postgresql`), which cannot use the GiST index, so each
request scans the whole US table. The parcel-scoped path, `find_buildings_in_bbox`, filters with `intersects` and
answered in production on 2026-10-01 (31 Overture records for one parcel).

**Asked:** an index-backed prefilter on both, such as `geometry__intersects` on the radius's bounding box (as
`find_buildings_in_bbox` does) before the exact distance filter, or `ST_DWithin` on a geography cast with a matching
index.

Until this is fixed, UrbanLens cannot show Overture buildings or places for any US pin, and each panel fetch or
boundary run probably starts one of these scans. The UrbanLens change was held on that account until REData fixed
it; it was ported onto `release/v_0_9_0` on 2026-10-05, so UrbanLens 0.9.0 needs REData 0.3.0 in production first.

A side effect on REData's own fan-out: `/points-of-interest/lookup/` runs its providers one after another, so any
lookup that includes `overture` waits for that scan. UrbanLens's "Cameras & Structures" panel asked for every
applicable provider, `overture` included; it now leaves `overture` out.

## 2. Columns the panel shows that REData does not ingest

- Buildings: `roof_shape` and `roof_material`. `overture_buildings/ingest.py`'s `_SELECT_COLUMNS` does not select
  them, although `OvertureBuilding.attributes`' docstring says roof and facade fields land there.
- Places: `operating_status`. `overture_places/ingest.py`'s `_SELECT_COLUMNS` omits it. UrbanLens marks a closed
  place "(closed)".

**Asked:** select them. Unpromoted columns land in `attributes`, which UrbanLens already reads, so no UrbanLens
change is needed when they appear. Until then US pins show no roof fields and no closed marker.

## 3. The `overture` providers cover more than the shards sync

Both `overture` providers are applicable wherever `is_usa_coordinates` holds. Its continental box reaches into
Canada, Mexico and the Bahamas. The sync covers the padded `shards.US_STATE_BBOXES`, which take in most border
cities but not all. Each of these is inside `is_usa_coordinates` and outside every state box:

| place | lat, lng |
|---|---|
| Hermosillo | 29.07, -110.96 |
| Nassau | 25.04, -77.35 |
| Sudbury | 46.49, -80.99 |
| Saguenay | 48.43, -71.06 |

REData answers these "ok" with no rows.

**Asked:** could the providers report `not_applicable` where no synced shard covers the point? REData tracks this as
its P97. UrbanLens no longer waits on it: since 2026-10-05 it asks REData only inside both `is_usa_coordinates` and a
vendored copy of `US_STATE_BBOXES` (`boundaries.redata_overture_shards`, re-vendored with
`bin/vendor_redata_schema.py --shards`), and reads the public release for these points. A shard REData has not synced
yet still answers "ok" with no rows, which UrbanLens cannot tell from an empty place.

## 4. Smaller items

- No migration adds `buildings:read` to existing keys (`0004`-`0006` backfill other scopes). Please confirm the
  staging and production UrbanLens keys hold it. UrbanLens reads a 403 as an outage, so a missing scope would keep
  retrying and never show anything.
- `docs/ENVIRONMENT.md` says an absent Overture database makes these endpoints report "no results, not an error".
  `overture.db.overture_db_errors_as_unavailable` turns an unreachable or unprovisioned database into
  `GatewayUnavailableError`, which `/buildings/` answers as a 503. UrbanLens handles both, but P110's decision was
  written from the doc. Which one is intended?

## Not asked

UrbanLens will not read Overture's public release inside the US to cover any of these gaps.
