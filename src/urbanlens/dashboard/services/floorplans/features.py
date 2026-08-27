"""A floorplan's drawable items as GeoJSON, filtered by viewport, storey and kind.

The map-facing counterpart to the document (``serialization.document_for``).
Two different questions:

- *"What is this building's plan?"* - the document: nested, whole, editable,
  and the shape the editor round-trips.
- *"What should I draw right now?"* - this: flat GeoJSON features for one
  storey inside one viewport, which is what a renderer (Leaflet, QGIS,
  anything that speaks GeoJSON) actually consumes.

This is where plan-local metres become WGS-84. The projection mirrors
``frontend/ts/shared/floorplan/coords.ts`` exactly - same Earth radius, same
equirectangular approximation about the plan origin - because the editor draws
in local metres and the map draws these features, and a disagreement between
the two would show up as a plan that shifts when you switch views.

Filtering happens in Python rather than the database: walls are stored as four
float columns, not geometry, so there is no spatial index to defer to. That is
the right trade - the bbox exists to bound what crosses the wire, and one
storey of one building is a few hundred rows to begin with.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanOpening

logger = logging.getLogger(__name__)

#: A plan can hold thousands of items; an unpaged read of all of them is
#: exactly what this endpoint exists to avoid.
MAX_FEATURES = 2000

#: The item types a caller may ask for, in draw order (walls under rooms under
#: markers) so a renderer that just appends gets sane stacking.
ITEM_TYPES = ("wall", "room", "marker")

#: Mean Earth radius (metres), WGS-84 authalic sphere. Must match coords.ts.
EARTH_RADIUS_M = 6371008.8


class PlanProjection:
    """Plan-local metres to WGS-84 for one plan origin.

    The server-side twin of ``coords.ts``'s class of the same name; keep the
    two in step.
    """

    def __init__(self, origin_lat: float, origin_lng: float) -> None:
        self.origin_lat = origin_lat
        self.origin_lng = origin_lng
        self.metres_per_deg_lat = (math.pi / 180) * EARTH_RADIUS_M
        self.metres_per_deg_lng = self.metres_per_deg_lat * math.cos(math.radians(origin_lat))

    def to_world(self, x: float, y: float) -> list[float]:
        """A local point as GeoJSON ``[lng, lat]``."""
        return [
            self.origin_lng + (x / self.metres_per_deg_lng if self.metres_per_deg_lng else 0.0),
            self.origin_lat + y / self.metres_per_deg_lat,
        ]


def _projection(floorplan: Floorplan) -> PlanProjection | None:
    """The plan's projection, or None when it has no origin to project about."""
    if floorplan.origin_lat is None or floorplan.origin_lng is None:
        return None
    return PlanProjection(float(floorplan.origin_lat), float(floorplan.origin_lng))


def _overlaps(points: list[list[float]], bbox: tuple[float, float, float, float]) -> bool:
    """Whether a feature's own bounds meet the requested viewport."""
    min_lng, min_lat, max_lng, max_lat = bbox
    lngs = [point[0] for point in points]
    lats = [point[1] for point in points]
    return not (max(lngs) < min_lng or min(lngs) > max_lng or max(lats) < min_lat or min(lats) > max_lat)


def _opening_state(opening: FloorplanOpening) -> str:
    """Whether this opening is presently secured, as one word.

    A door may carry several locks, and the useful answer is about the door
    rather than about any one of them: a padlock on and a deadbolt off still
    means the door does not open. Only when every lock is known to be off does
    the door read as unlocked; with no locks recorded, or none of them known,
    the honest answer is that nobody has said.

    Args:
        opening: The opening, with its locks prefetched.

    Returns:
        ``"locked"``, ``"unlocked"`` or ``"unknown"``.
    """
    states = [lock.state for lock in opening.locks.all()]
    if any(state == "locked" for state in states):
        return "locked"
    if states and all(state == "unlocked" for state in states):
        return "unlocked"
    return "unknown"


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
            overlapping it.
        level: Restrict to one storey by its level number.
        kind: Restrict walls to one :class:`FloorplanWallKind`, or markers to
            one :class:`FloorplanMarkerKind`.
        item_types: Which of wall/room/marker to include.
        limit: Hard cap on features returned.

    Returns:
        A ``FeatureCollection`` dict, with ``truncated`` set on its top level
        when the cap was reached - silence about a cut-off list reads as
        "that's everything", which it would not be. Empty when the plan has no
        origin, since nothing can be placed on a map without one.
    """
    from urbanlens.dashboard.models.floorplans.model import FloorplanFloor, FloorplanMarker, FloorplanRoomSeed, FloorplanWall

    projection = _projection(floorplan)
    features: list[dict[str, Any]] = []
    truncated = False
    if projection is None:
        return {"type": "FeatureCollection", "features": features, "count": 0, "truncated": False, "floorplan": str(floorplan.uuid)}

    floors = FloorplanFloor.objects.filter(floorplan=floorplan)
    if level is not None:
        floors = floors.filter(level=level)
    floor_levels = dict(floors.values_list("pk", "level"))
    if not floor_levels:
        return {"type": "FeatureCollection", "features": features, "count": 0, "truncated": False, "floorplan": str(floorplan.uuid)}

    def add(feature: dict[str, Any], points: list[list[float]]) -> bool:
        """Append a feature unless filtered out or over the cap."""
        nonlocal truncated
        if bbox is not None and not _overlaps(points, bbox):
            return True
        if len(features) >= limit:
            truncated = True
            return False
        features.append(feature)
        return True

    if "wall" in item_types:
        # Locks come with the openings, not one query per door: a renderer's
        # first question about a door is whether it opens, and asking it per
        # opening would be a query per opening.
        walls = FloorplanWall.objects.filter(floor_id__in=floor_levels).prefetch_related("openings__locks")
        if kind:
            walls = walls.filter(kind=kind)
        for wall in walls:
            line = [projection.to_world(wall.ax, wall.ay), projection.to_world(wall.bx, wall.by)]
            feature = {
                "type": "Feature",
                "id": str(wall.uuid),
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": {
                    "item_type": "wall",
                    "uuid": str(wall.uuid),
                    "name": wall.name,
                    "kind": wall.kind,
                    "thickness": wall.thickness,
                    "condition": wall.condition,
                    "sort_order": wall.sort_order,
                    "level": floor_levels.get(wall.floor_id),
                    # Openings ride with their wall rather than as features of
                    # their own: they have no independent position, and a
                    # renderer needs the wall to draw one at all.
                    "openings": [
                        {
                            "uuid": str(opening.uuid),
                            "kind": opening.kind,
                            "t_start": opening.t_start,
                            "t_end": opening.t_end,
                            "swing": opening.swing,
                            "sill_meters": opening.sill_meters,
                            # One word for what a reader wants to know about a
                            # door, rather than the locks themselves: a lock's
                            # type, condition and what opens it are the
                            # document's business, and a map wants to colour a
                            # door. Locked if any lock on it is - a door with a
                            # padlock on and a deadbolt off is locked.
                            "lock_state": _opening_state(opening),
                        }
                        for opening in wall.openings.all()
                    ],
                },
            }
            if not add(feature, line):
                break

    if "room" in item_types and not truncated:
        for room in FloorplanRoomSeed.objects.filter(floor_id__in=floor_levels):
            point = projection.to_world(room.x, room.y)
            feature = {
                "type": "Feature",
                "id": str(room.uuid),
                "geometry": {"type": "Point", "coordinates": point},
                "properties": {
                    "item_type": "room",
                    "uuid": str(room.uuid),
                    "name": room.name,
                    "condition": room.condition,
                    "sort_order": room.sort_order,
                    "level": floor_levels.get(room.floor_id),
                },
            }
            if not add(feature, [point]):
                break

    if "marker" in item_types and not truncated:
        markers = FloorplanMarker.objects.filter(floor_id__in=floor_levels)
        if kind:
            markers = markers.filter(kind=kind)
        for marker in markers:
            point = projection.to_world(marker.x, marker.y)
            feature = {
                "type": "Feature",
                "id": str(marker.uuid),
                "geometry": {"type": "Point", "coordinates": point},
                "properties": {
                    "item_type": "marker",
                    "uuid": str(marker.uuid),
                    "name": marker.name,
                    "kind": marker.kind,
                    "condition": marker.condition,
                    "sort_order": marker.sort_order,
                    "facing_degrees": marker.facing_degrees,
                    "connector_id": marker.connector_id,
                    "level": floor_levels.get(marker.floor_id),
                },
            }
            if not add(feature, [point]):
                break

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
        ``[min_lng, min_lat, max_lng, max_lat]``, or None when the plan has no
        origin or nothing placed.
    """
    from urbanlens.dashboard.models.floorplans.model import FloorplanFloor, FloorplanMarker, FloorplanRoomSeed, FloorplanWall

    projection = _projection(floorplan)
    if projection is None:
        return None

    floor_ids = list(FloorplanFloor.objects.filter(floorplan=floorplan).values_list("pk", flat=True))
    if not floor_ids:
        return None

    xs: list[float] = []
    ys: list[float] = []
    for ax, ay, bx, by in FloorplanWall.objects.filter(floor_id__in=floor_ids).values_list("ax", "ay", "bx", "by"):
        xs.extend((ax, bx))
        ys.extend((ay, by))
    for model in (FloorplanRoomSeed, FloorplanMarker):
        for x, y in model.objects.filter(floor_id__in=floor_ids).values_list("x", "y"):
            xs.append(x)
            ys.append(y)
    if not xs:
        return None

    low = projection.to_world(min(xs), min(ys))
    high = projection.to_world(max(xs), max(ys))
    return [low[0], low[1], high[0], high[1]]
