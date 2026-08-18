"""A floorplan's drawable items as GeoJSON, filtered by viewport, storey and kind.

The map-facing counterpart to the document (``serialization.document_for``),
mirroring REData's ``/floorplans/{uuid}/features/``. Two different questions:

- *"What is this building's plan?"* - the document: nested, whole, editable,
  and the shape the editor round-trips.
- *"What should I draw right now?"* - this: flat GeoJSON features for one
  storey inside one viewport, which is what a renderer (Leaflet, QGIS,
  anything that speaks GeoJSON) actually consumes.

A plan can hold tens of thousands of items across a dozen storeys, so the
whole-document read is the wrong request for a map, and an unbounded feature
read is the request this endpoint exists to make unnecessary: the bbox filter
runs in the database (``geometry__bboverlaps``, which uses the spatial index)
and the result is capped.

Every feature carries its ``uuid`` and item type, so a feature a user clicks
can be found in the document and edited without a second identifier scheme.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.floorplans.model import Floorplan

logger = logging.getLogger(__name__)

#: A plan can hold tens of thousands of items; an unpaged read of all of them
#: is exactly what this endpoint exists to avoid.
MAX_FEATURES = 2000

#: The item types a caller may ask for, in draw order (floors under rooms
#: under elements) so a renderer that just appends gets sane stacking.
ITEM_TYPES = ("floor", "room", "element")


def _feature(row, item_type: str, extra: dict[str, Any]) -> dict[str, Any] | None:
    """One GeoJSON feature, or None when the row has no geometry to draw."""
    if row.geometry is None:
        return None
    return {
        "type": "Feature",
        "id": str(row.uuid),
        "geometry": json.loads(row.geometry.geojson),
        "properties": {
            "item_type": item_type,
            "uuid": str(row.uuid),
            "name": getattr(row, "name", "") or "",
            "condition": row.condition,
            "material": getattr(row, "material", "") or "",
            "sort_order": row.sort_order,
            **extra,
        },
    }


def feature_collection(
    floorplan: Floorplan,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    level: int | None = None,
    kind: str = "",
    item_types: tuple[str, ...] = ITEM_TYPES,
    limit: int = MAX_FEATURES,
) -> dict[str, Any]:
    """Assemble one plan's drawable items as a GeoJSON FeatureCollection.

    Args:
        floorplan: The plan version to read.
        bbox: ``(min_lng, min_lat, max_lng, max_lat)`` in WGS-84; only items
            overlapping it. Filtered in the database, on the spatial index.
        level: Restrict to one storey by its level number.
        kind: Restrict elements to one :class:`FloorplanElementKind`.
        item_types: Which of floor/room/element to include.
        limit: Hard cap on features returned.

    Returns:
        A ``FeatureCollection`` dict, with ``truncated`` set on its top level
        when the cap was reached - silence about a cut-off list reads as
        "that's everything", which it would not be.
    """
    from django.contrib.gis.geos import Polygon

    from urbanlens.dashboard.models.floorplans.model import FloorplanElement, FloorplanFloor, FloorplanRoom

    box = Polygon.from_bbox(bbox) if bbox is not None else None
    features: list[dict[str, Any]] = []
    truncated = False

    def collect(queryset, item_type: str, extra_for) -> None:
        nonlocal truncated
        if truncated or item_type not in item_types:
            return
        if box is not None:
            queryset = queryset.filter(geometry__bboverlaps=box)
        # One past the cap, so "there is more" is a fact rather than a guess.
        for row in queryset[: limit - len(features) + 1]:
            if len(features) >= limit:
                truncated = True
                return
            feature = _feature(row, item_type, extra_for(row))
            if feature is not None:
                features.append(feature)

    floors = FloorplanFloor.objects.filter(floorplan=floorplan)
    if level is not None:
        floors = floors.filter(level=level)
    floor_levels = dict(floors.values_list("pk", "level"))

    collect(floors.filter(geometry__isnull=False), "floor", lambda row: {"level": row.level})

    rooms = FloorplanRoom.objects.filter(floor__floorplan=floorplan, geometry__isnull=False)
    if level is not None:
        rooms = rooms.filter(floor__level=level)
    collect(rooms, "room", lambda row: {"level": floor_levels.get(row.floor_id)})

    elements = FloorplanElement.objects.filter(floorplan=floorplan, geometry__isnull=False)
    if level is not None:
        elements = elements.filter(floor__level=level)
    if kind:
        elements = elements.filter(kind=kind)
    collect(elements, "element", lambda row: {"kind": row.kind, "level": floor_levels.get(row.floor_id)})

    return {
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
        "truncated": truncated,
        "floorplan": str(floorplan.uuid),
    }


def bounds_of(floorplan: Floorplan) -> list[float] | None:
    """The plan's overall extent, so a client can decide whether to draw it at all.

    Args:
        floorplan: The plan version.

    Returns:
        ``[min_lng, min_lat, max_lng, max_lat]``, or None when nothing in the
        plan has geometry.
    """
    from django.contrib.gis.db.models import Extent
    from django.db.models import Q

    from urbanlens.dashboard.models.floorplans.model import FloorplanElement, FloorplanFloor, FloorplanRoom

    extents = [
        FloorplanFloor.objects.filter(floorplan=floorplan).aggregate(extent=Extent("geometry"))["extent"],
        FloorplanRoom.objects.filter(floor__floorplan=floorplan).aggregate(extent=Extent("geometry"))["extent"],
        FloorplanElement.objects.filter(Q(floorplan=floorplan)).aggregate(extent=Extent("geometry"))["extent"],
    ]
    present = [extent for extent in extents if extent is not None]
    if not present:
        return None
    return [
        min(extent[0] for extent in present),
        min(extent[1] for extent in present),
        max(extent[2] for extent in present),
        max(extent[3] for extent in present),
    ]
