# REData's building lookup returns an OSM multipolygon building as its centre point

- **Status: OPEN as of 2026-10-01.** Found by UrbanLens's P182 investigation on a v0.8.0 dev environment
  (`v080e2e`). The record below is from the `parcel_buildings` cache that environment wrote from production
  REData (`https://redata.urbanlens.org`) on 2026-10-01.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What UrbanLens received

`lookup_buildings` for the Hudson River State Hospital parcel (41.73328, -73.92812) returned Kirkbride with a point
for its geometry, although its only source says it is a multipolygon relation:

```
"ref": "overpass:relation/10813427", "name": "Kirkbride (Admin Building)", "match_scope": "parcel",
"geometry": {"type": "Point", "coordinates": [-73.92736, 41.73266]}, "residual_geometry": null,
"parent_ref": null, "child_refs": [], "overlap_refs": [],
"sources": [{"source": "overpass", "geometry": {"type": "Point", ...},
             "attributes": {"type": "multipolygon", "osm_id": "relation/10813427", "building": "yes", ...}}]
```

In the same answer, every one of the 31 Overture records and 17 of the CRIS records carry a Polygon. This is the
only Overpass record, and it is the one point among the footprints. The point is the relation's centre, which
falls in one of Kirkbride's courtyards.

`parcels/services/overpass/gateway.py` asks for `out geom tags` and `core/services/geojson.py` builds multipolygons
from relation members, so the bbox path looks right. `overpass/lookup.py` also has an `out center tags qt` query,
and this record may have come through that one instead. That is a guess from reading `main`, not a trace.

## Why it matters to UrbanLens

UrbanLens makes a building place from each record, with the record's footprint as the place's outline. A place
with no outline can never be resolved onto, so a pin at Kirkbride's point lands on the parcel, and Kirkbride's
outline is never drawn (UrbanLens P182).

## Asked

Could a building that comes from an OSM way or multipolygon relation carry its polygon in `geometry`, as the
Overture records do? If one lookup path only has the centre, could that path fetch the geometry, or the record
link the Overture footprint it overlaps (`overlap_refs`)?

## Not asked

UrbanLens will not fetch geometry from Overpass itself. Until REData changes, the building place keeps no outline,
and pins at its point stand on the parcel.
