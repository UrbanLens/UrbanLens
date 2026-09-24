"""A floorplan's drawable items as GeoJSON, filtered by viewport, storey and kind."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from django.db.models import F, Max, Min, Q, QuerySet
from django.db.models.functions import Abs, Greatest, Least

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
    """Plan-local metres to WGS-84 for one plan origin."""

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

    def local_bounds(self, bbox: tuple[float, float, float, float]) -> LocalBounds:
        """A WGS-84 viewport as the plan-local rectangle it covers.

        The projection is linear in each axis, so the rectangle is exact, and an item overlaps the viewport
        exactly when its local bounds overlap this.

        Args:
            bbox: ``(min_lng, min_lat, max_lng, max_lat)``.

        Returns:
            The local-metre bounds; x is unbounded when the origin sits on a pole and longitude means nothing.
        """
        min_lng, min_lat, max_lng, max_lat = bbox
        min_y = (min_lat - self.origin_lat) * self.metres_per_deg_lat
        max_y = (max_lat - self.origin_lat) * self.metres_per_deg_lat
        if not self.metres_per_deg_lng:
            return LocalBounds(None, min_y, None, max_y)
        return LocalBounds((min_lng - self.origin_lng) * self.metres_per_deg_lng, min_y, (max_lng - self.origin_lng) * self.metres_per_deg_lng, max_y)


class LocalBounds:
    """A plan-local rectangle in metres; a None x edge means unbounded."""

    def __init__(self, min_x: float | None, min_y: float, max_x: float | None, max_y: float) -> None:
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y

    def segments(self) -> Q:
        """Rows whose segment ``(ax, ay)-(bx, by)`` has bounds overlapping this rectangle."""
        condition = Q(low_y__lte=self.max_y, high_y__gte=self.min_y)
        if self.min_x is not None and self.max_x is not None:
            condition &= Q(low_x__lte=self.max_x, high_x__gte=self.min_x)
        return condition

    def points(self) -> Q:
        """Rows whose point ``(x, y)`` falls inside this rectangle."""
        condition = Q(y__gte=self.min_y, y__lte=self.max_y)
        if self.min_x is not None and self.max_x is not None:
            condition &= Q(x__gte=self.min_x, x__lte=self.max_x)
        return condition


def _segment_extent(walls: QuerySet) -> QuerySet:
    return walls.annotate(low_x=Least("ax", "bx"), high_x=Greatest("ax", "bx"), low_y=Least("ay", "by"), high_y=Greatest("ay", "by"))


def ground_level(floorplan: Floorplan) -> int | None:
    """The storey a caller that names none is shown: level 0, else the one nearest it.

    Args:
        floorplan: The plan version.

    Returns:
        The level, or None when the plan has no floors.
    """
    from urbanlens.dashboard.models.floorplans.model import FloorplanFloor

    return FloorplanFloor.objects.filter(floorplan=floorplan).order_by(Abs("level"), "level").values_list("level", flat=True).first()


def _projection(floorplan: Floorplan) -> PlanProjection | None:
    """The plan's projection, or None when it has no origin to project about."""
    if floorplan.origin_lat is None or floorplan.origin_lng is None:
        return None
    return PlanProjection(float(floorplan.origin_lat), float(floorplan.origin_lng))


def _opening_state(opening: FloorplanOpening) -> str:
    """Whether this opening is presently secured, as one word.
    A door may carry several locks, and the useful answer is about the door rather than about any one of them: a padlock on and a deadbolt off still means the door does not open.

    Args:
        opening: The opening, with its locks prefetched.

    Returns:
        ``"locked"``, ``"unlocked"`` or ``"unknown"``."""
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
        bbox: ``(min_lng, min_lat, max_lng, max_lat)`` in WGS-84; only items overlapping it.
        level: Restrict to one storey by its level number.
        kind: Restrict walls to one :class:`FloorplanWallKind`, or markers to one :class:`FloorplanMarkerKind`.
        item_types: Which of wall/room/marker to include.
        limit: Hard cap on features returned.

    Returns:
        A ``FeatureCollection`` dict, with ``truncated`` set on its top level when the cap was reached - silence about a cut-off list reads as "that's everything", which it would not be.

    The viewport, the kind and the cap are applied in the query, so a read costs the rows it returns rather
    than every row on the selected floors."""
    from django.db.models import prefetch_related_objects

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

    window = projection.local_bounds(bbox) if bbox is not None else None

    def take(rows: QuerySet) -> list[Any]:
        """At most the remaining budget of *rows*, noting when there were more."""
        nonlocal truncated
        remaining = limit - len(features)
        fetched = list(rows.order_by("floor_id", "sort_order", "pk")[: remaining + 1]) if remaining > 0 else list(rows[:1])
        if len(fetched) > remaining:
            truncated = True
            return fetched[: max(remaining, 0)]
        return fetched

    if "wall" in item_types:
        walls_qs = _segment_extent(FloorplanWall.objects.filter(floor_id__in=floor_levels))
        if kind:
            walls_qs = walls_qs.filter(kind=kind)
        if window is not None:
            walls_qs = walls_qs.filter(window.segments())
        walls = take(walls_qs)
        # Locks come with the openings, not one query per door - and only for the walls kept.
        prefetch_related_objects(walls, "openings__locks")
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
                            # One word for what a reader wants to know about a door, rather than the
                            # locks themselves: a lock's type, condition and what opens it are the
                            # document's business, and a map wants to colour a door.
                            # Locked if any lock on it is - a door with a padlock on and a deadbolt
                            "lock_state": _opening_state(opening),
                        }
                        for opening in wall.openings.all()
                    ],
                },
            }
            features.append(feature)

    if "room" in item_types and not truncated:
        rooms = FloorplanRoomSeed.objects.filter(floor_id__in=floor_levels)
        if window is not None:
            rooms = rooms.filter(window.points())
        for room in take(rooms):
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
            features.append(feature)

    if "marker" in item_types and not truncated:
        markers = FloorplanMarker.objects.filter(floor_id__in=floor_levels)
        if kind:
            markers = markers.filter(kind=kind)
        if window is not None:
            markers = markers.filter(window.points())
        for marker in take(markers):
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
            features.append(feature)

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
        ``[min_lng, min_lat, max_lng, max_lat]``, or None when the plan has no origin or nothing placed."""
    from urbanlens.dashboard.models.floorplans.model import FloorplanFloor, FloorplanMarker, FloorplanRoomSeed, FloorplanWall

    projection = _projection(floorplan)
    if projection is None:
        return None

    floors = FloorplanFloor.objects.filter(floorplan=floorplan).values("pk")
    extents = [
        FloorplanWall.objects.filter(floor_id__in=floors).aggregate(min_x=Min(Least("ax", "bx")), min_y=Min(Least("ay", "by")), max_x=Max(Greatest("ax", "bx")), max_y=Max(Greatest("ay", "by"))),
        *(model.objects.filter(floor_id__in=floors).aggregate(min_x=Min("x"), min_y=Min("y"), max_x=Max(F("x")), max_y=Max(F("y"))) for model in (FloorplanRoomSeed, FloorplanMarker)),
    ]
    xs = [value for extent in extents for value in (extent["min_x"], extent["max_x"]) if value is not None]
    ys = [value for extent in extents for value in (extent["min_y"], extent["max_y"]) if value is not None]
    if not xs:
        return None

    low = projection.to_world(min(xs), min(ys))
    high = projection.to_world(max(xs), max(ys))
    return [low[0], low[1], high[0], high[1]]
