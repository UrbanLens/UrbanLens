"""One marker per physical building, from a parcel's building records.

REData reports buildings it could not reconcile as separate records - an ``overlap_refs`` pair, two points a
few metres apart - and the Overpass fallback reconciles nothing. A marker per record would put two pins on
one structure, so records that describe the same building on the ground are grouped here, and every consumer
(auto-nest, the restructure dialog, the "Buildings on this Property" panel, the wiki mirror) places and
matches markers per group rather than per record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Protocol

from django.contrib.gis.geos import Point
from django.contrib.gis.geos.error import GEOSException

from urbanlens.dashboard.services.locations import site_scope

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.contrib.gis.geos import GEOSGeometry

logger = logging.getLogger(__name__)

#: Slack on :data:`site_scope.BUILDING_MATCH_METERS` so two markers kept apart here are still apart once
#: ``Location`` rounds their coordinates to six decimal places (~0.1 m).
_ROUNDING_SLACK_METERS = 0.5

#: Two sibling footprints sharing at least this fraction of the smaller one's area are one building.
_FOOTPRINT_OVERLAP_SAME = 0.5


class Marker(Protocol):
    """Anything standing at a coordinate that a cluster can be matched against (a pin, a wiki, a record)."""

    @property
    def effective_latitude(self) -> float: ...

    @property
    def effective_longitude(self) -> float: ...


@dataclass(frozen=True, slots=True)
class SweptBuilding:
    """Where auto-nest once placed a building's pin, kept after the pin itself is deleted."""

    effective_latitude: float
    effective_longitude: float
    ref: str = ""

    def to_json(self) -> dict[str, Any]:
        """The shape stored on ``Pin.auto_nested_buildings``."""
        return {"latitude": self.effective_latitude, "longitude": self.effective_longitude, "ref": self.ref}

    @classmethod
    def from_json(cls, value: Any) -> SweptBuilding | None:
        """Parse one stored entry, or None when it is malformed."""
        if not isinstance(value, dict):
            return None
        try:
            return cls(float(value["latitude"]), float(value["longitude"]), str(value.get("ref") or ""))
        except (KeyError, TypeError, ValueError):
            return None


def _footprint(building: dict[str, Any]) -> GEOSGeometry | None:
    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint

    return building_footprint(building)


def _record_point(building: dict[str, Any]) -> tuple[float, float] | None:
    latitude, longitude = building.get("latitude"), building.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        return float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None


def marker_point(building: dict[str, Any], boundary: GEOSGeometry | None = None) -> tuple[float, float] | None:
    """Where a building's marker goes: on the building, and on the property.

    A record's own coordinate is its footprint's centroid, which can fall outside a concave footprint (a
    U-shaped ward block) or outside the parcel (a building straddling the lot line). A marker there would
    neither be matched back to its building nor sit on the property it was nested under.

    Args:
        building: A cached building record.
        boundary: The property's real boundary, when known.

    Returns:
        ``(latitude, longitude)``, or None when the record has no usable coordinate at all.
    """
    point = _record_point(building)
    footprint = _footprint(building)
    if footprint is None:
        return point

    region = footprint
    if boundary is not None:
        try:
            clipped = footprint.intersection(boundary)
        except GEOSException:
            logger.debug("building_clusters: footprint/boundary intersection failed", exc_info=True)
        else:
            if not clipped.empty and clipped.dims == 2 and clipped.area > 0:
                region = clipped
    if point is not None and region.intersects(Point(point[1], point[0], srid=4326)):
        return point
    try:
        surface = region.point_on_surface
    except GEOSException:
        return point
    return float(surface.y), float(surface.x)


@dataclass(eq=False, slots=True)
class BuildingCluster:
    """Records that describe one building, and where its marker stands."""

    representative: dict[str, Any]
    latitude: float
    longitude: float
    footprint: GEOSGeometry | None = None
    members: list[dict[str, Any]] = field(default_factory=list)
    parent: BuildingCluster | None = None
    depth: int = 0
    has_children: bool = False
    _footprints: list[GEOSGeometry] = field(default_factory=list)
    _points: list[tuple[float, float]] = field(default_factory=list)

    @property
    def effective_latitude(self) -> float:
        """Marker latitude (the :class:`Marker` protocol)."""
        return self.latitude

    @property
    def effective_longitude(self) -> float:
        """Marker longitude (the :class:`Marker` protocol)."""
        return self.longitude

    @property
    def refs(self) -> set[str]:
        """Every member's REData ``ref``."""
        return {ref for member in self.members if (ref := str(member.get("ref") or "").strip())}

    @property
    def name(self) -> str:
        """The first usable marker name across members, representative first (see ``building_name``)."""
        from urbanlens.dashboard.services.pins.pin_restructure import building_name

        return next((name for member in self.members if (name := building_name(member))), "")

    def add(self, building: dict[str, Any], footprint: GEOSGeometry | None) -> None:
        """Absorb another record describing this building."""
        self.members.append(building)
        if footprint is not None:
            self._footprints.append(footprint)
        elif (point := _record_point(building)) is not None:
            self._points.append(point)

    def distance_to(self, latitude: float, longitude: float) -> float:
        """Metres from this cluster's marker to a coordinate."""
        return site_scope.meters_between(self.latitude, self.longitude, latitude, longitude)

    def covers(self, latitude: float, longitude: float) -> bool:
        """Whether a marker at this coordinate already stands for this building.

        Args:
            latitude: The marker's latitude.
            longitude: The marker's longitude.

        Returns:
            True when the point is on any member's footprint, or within the app's building-match radius of
            this cluster's marker or of a member that published only a point.
        """
        point = Point(float(longitude), float(latitude), srid=4326)
        if any(footprint.contains(point) or footprint.touches(point) for footprint in self._footprints):
            return True
        if self.distance_to(latitude, longitude) <= site_scope.BUILDING_MATCH_METERS:
            return True
        return any(site_scope.meters_between(lat, lng, latitude, longitude) <= site_scope.BUILDING_MATCH_METERS for lat, lng in self._points)


def _overlap_refs(building: dict[str, Any]) -> set[str]:
    return {str(ref) for ref in (building.get("overlap_refs") or []) if ref}


def _same_building(building: dict[str, Any], footprint: GEOSGeometry | None, point: tuple[float, float], cluster: BuildingCluster) -> bool:
    """Whether a sibling record describes the building a cluster's representative already stands for.

    Compared against the representative only, never every member, so a row of terraced outbuildings each
    close to the next cannot chain into one cluster.
    """
    representative = cluster.representative
    ref = str(building.get("ref") or "")
    representative_ref = str(representative.get("ref") or "")
    if (ref and ref in _overlap_refs(representative)) or (representative_ref and representative_ref in _overlap_refs(building)):
        return True
    if cluster.distance_to(*point) < site_scope.BUILDING_MATCH_METERS + _ROUNDING_SLACK_METERS:
        return True

    # A point left inside a sibling's footprint is not grouped: REData merges that pair itself unless both
    # come from one source, which it treats as two buildings.
    representative_footprint = cluster.footprint
    if representative_footprint is None or footprint is None:
        return False
    try:
        shared = footprint.intersection(representative_footprint).area
    except GEOSException:
        return False
    smaller = min(footprint.area, representative_footprint.area)
    return smaller > 0 and shared / smaller >= _FOOTPRINT_OVERLAP_SAME


@dataclass(slots=True)
class _Entry:
    building: dict[str, Any]
    point: tuple[float, float]
    footprint: GEOSGeometry | None
    index: int

    def priority(self) -> tuple:
        """Representatives first: a footprint to seed walls from, then a name, then the larger structure."""
        area = self.footprint.area if self.footprint is not None else 0.0
        return (self.footprint is None, not self.building.get("name"), -area, self.index)


def cluster_buildings(buildings: Sequence[dict[str, Any]], boundary: GEOSGeometry | None = None) -> list[BuildingCluster]:
    """Group building records into one cluster per physical building, parents before their children.

    Records are only ever grouped with siblings - REData's ``parent_ref`` nesting is a verified "these are
    different buildings", so a wing never merges into the block it is part of. Siblings join when REData
    reports them as overlapping, when their footprints mostly coincide, or when their markers would stand
    within ``BUILDING_MATCH_METERS`` of each other.

    Args:
        buildings: On-property building records (already boundary-filtered by the caller).
        boundary: The property's real boundary, used to keep markers on the property.

    Returns:
        Clusters in tree order; a record with no usable coordinate is left out.
    """
    entries: list[_Entry] = []
    for index, building in enumerate(buildings):
        point = marker_point(building, boundary)
        if point is not None:
            entries.append(_Entry(building, point, _footprint(building), index))

    known_refs = {ref for entry in entries if (ref := str(entry.building.get("ref") or "").strip())}
    children_of: dict[str, list[_Entry]] = {}
    roots: list[_Entry] = []
    for entry in entries:
        parent_ref = str(entry.building.get("parent_ref") or "").strip()
        own_ref = str(entry.building.get("ref") or "").strip()
        if parent_ref and parent_ref in known_refs and parent_ref != own_ref:
            children_of.setdefault(parent_ref, []).append(entry)
        else:
            roots.append(entry)

    ordered: list[BuildingCluster] = []
    placed: set[int] = set()

    def cluster_level(level: list[_Entry], parent: BuildingCluster | None) -> None:
        clusters: list[BuildingCluster] = []
        for entry in sorted(level, key=_Entry.priority):
            if id(entry.building) in placed:
                continue
            placed.add(id(entry.building))
            linked = [cluster for cluster in clusters if _same_building(entry.building, entry.footprint, entry.point, cluster)]
            if linked:
                min(linked, key=lambda cluster: cluster.distance_to(*entry.point)).add(entry.building, entry.footprint)
                continue
            cluster = BuildingCluster(
                representative=entry.building,
                latitude=entry.point[0],
                longitude=entry.point[1],
                footprint=entry.footprint,
                parent=parent,
                depth=parent.depth + 1 if parent else 0,
            )
            cluster.add(entry.building, entry.footprint)
            clusters.append(cluster)
        for cluster in clusters:
            ordered.append(cluster)
            children = [child for ref in sorted(cluster.refs) for child in children_of.get(ref, [])]
            if children:
                cluster_level(children, cluster)
            cluster.has_children = any(other.parent is cluster for other in ordered)

    cluster_level(roots, None)
    # Whatever is left sits in a parent_ref cycle; place it at the top rather than lose it.
    leftover = [entry for entry in entries if id(entry.building) not in placed]
    if leftover:
        cluster_level(leftover, None)
    return ordered


def distinct_building_count(clusters: Sequence[BuildingCluster]) -> int:
    """How many physical buildings the clusters describe: an envelope over others is not an extra one."""
    return sum(1 for cluster in clusters if not cluster.has_children)


def _same_point(a: Marker, b: Marker) -> bool:
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    return quantize_coordinate(a.effective_latitude, "latitude") == quantize_coordinate(b.effective_latitude, "latitude") and quantize_coordinate(a.effective_longitude, "longitude") == quantize_coordinate(b.effective_longitude, "longitude")


def match_clusters[M: Marker](clusters: Sequence[BuildingCluster], markers: Sequence[M]) -> tuple[dict[int, M], list[M]]:
    """Pair clusters with the markers already standing for them, each marker claimed at most once.

    A marker placed exactly on a cluster's marker point is claimed first, so a re-sweep recognises its own
    earlier pins however the records' refs have changed since. The rest are paired closest first, most deeply
    nested cluster first, so a pin on a chapel is the chapel's rather than the envelope's around it, and a pin
    standing on a building's own point is that building's even when a larger neighbour's footprint covers it.

    Args:
        clusters: Clusters from :func:`cluster_buildings`.
        markers: Existing pins, wikis or :class:`SweptBuilding` entries.

    Returns:
        ``(matched, unmatched)``: cluster index to its marker, and the markers nothing claimed.
    """
    available = list(markers)
    matched: dict[int, M] = {}
    for index, cluster in enumerate(clusters):
        exact = next((marker for marker in available if _same_point(marker, cluster)), None)
        if exact is not None:
            matched[index] = exact
            available.remove(exact)

    pairs = sorted(
        (-cluster.depth, cluster.distance_to(marker.effective_latitude, marker.effective_longitude), index, position)
        for index, cluster in enumerate(clusters)
        if index not in matched
        for position, marker in enumerate(available)
        if cluster.covers(marker.effective_latitude, marker.effective_longitude)
    )
    claimed: set[int] = set()
    for _depth, _distance, index, position in pairs:
        if index not in matched and position not in claimed:
            matched[index] = available[position]
            claimed.add(position)
    return matched, [marker for position, marker in enumerate(available) if position not in claimed]
