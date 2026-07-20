"""Shared data types for the satellite imagery and street-view carousels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import itertools
import json
from typing import TYPE_CHECKING, ClassVar

from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Point, Polygon
from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.gateway import Gateway, Service

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable


BBox = tuple[float, float, float, float]
DEFAULT_BBOX_DEGREES = 0.00045
BOUNDARY_LOOKUP_BBOX_DEGREES = 0.001
MAX_DEFAULT_BOUNDARY_AREA_DEGREES = 0.02

_CACHE_MISS = object()


def _external_data_cache_seconds() -> int:
    """Seconds to cache satellite/street-view imagery, per the site's configured minimum."""
    from urbanlens.dashboard.models.site_settings import SiteSettings

    return SiteSettings.get_current().external_data_cache_days * 86400


@dataclass(frozen=True)
class SatelliteSlide:
    """A single slide in the satellite imagery carousel.

    Attributes:
        img_src: Absolute URL or ``data:`` URI for the image.
        source: Human-readable provider name (e.g. ``"Google Maps"``).
        date: Human-readable date or year (e.g. ``"Current"`` or ``"2019"``).
        detail: One-line description of resolution / coverage.
    """

    img_src: str
    source: str
    date: str
    detail: str


@dataclass(frozen=True)
class StreetViewSlide:
    """A single street-level image result.

    Attributes:
        img_src: Absolute URL or ``data:`` URI for the image.
        source: Human-readable provider name (e.g. ``"Google Street View"``).
        date: Human-readable capture date (e.g. ``"2022-06"`` or ``"Unknown"``).
        heading: Camera heading in degrees (0-360, 0 = north). ``None`` if unknown.
        latitude: Actual image capture latitude. ``None`` if unknown.
        longitude: Actual image capture longitude. ``None`` if unknown.
    """

    img_src: str
    source: str
    date: str
    heading: float | None = field(default=None)
    latitude: float | None = field(default=None)
    longitude: float | None = field(default=None)


class SatelliteViewProvider(Gateway, ABC):
    @abstractmethod
    def _generate_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = -1) -> Generator[SatelliteSlide]: ...

    def get_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = 5) -> tuple[list[SatelliteSlide], bool]:
        cache_key = make_cache_key(f"satellite_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        cached = cache.get(cache_key, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached, True

        slides = []
        for slide in self._generate_satellite_slides(latitude, longitude, zoom=zoom, width=width, height=height):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        cache.set(cache_key, slides, _external_data_cache_seconds())
        return slides, False


class StreetViewProvider(Gateway, ABC):
    @abstractmethod
    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]: ...

    def get_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> tuple[list[StreetViewSlide], bool]:
        cache_key = make_cache_key(f"street_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        cached = cache.get(cache_key, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached, True

        slides = []
        for slide in self._generate_street_view_slides(latitude, longitude, radius=radius):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        cache.set(cache_key, slides, _external_data_cache_seconds())
        return slides, False


class BoundaryProvider(Service, ABC):
    """Provider interface for default-boundary data sources.

    Providers declare which kind of boundary they yield via ``boundary_kind``
    ("property" or "building", matching :class:`BoundaryType` values). Sources
    that can't say per-feature must declare "property" - ambiguity is always
    treated as a property boundary. Providers that can distinguish per feature
    (e.g. Overpass) override :meth:`get_typed_boundaries` instead.
    """

    #: The boundary type this provider's ``get_boundary`` result describes.
    boundary_kind: ClassVar[str] = "property"

    @abstractmethod
    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """Return a polygon boundary for the coordinate, or None to allow fallback."""
        ...

    def get_typed_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> dict[str, Polygon | None]:
        """Return this provider's boundaries keyed by boundary type.

        The default implementation wraps :meth:`get_boundary` under
        ``boundary_kind``. Providers that can classify features per type
        override this to return both kinds from one upstream query.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Optional place name for name-aware providers.

        Returns:
            Mapping of boundary type value to polygon (or None).
        """
        return {self.boundary_kind: self.get_boundary(latitude, longitude, name=name)}


@dataclass(slots=True)
class StaticBoundaryProvider(BoundaryProvider):
    """Deterministic bbox provider, kept for tests and explicit callers.

    No longer part of the default provider chain: when no provider finds a
    boundary, the effective property boundary falls back to the default circle
    around the location's coordinates instead of a static bbox.
    """

    service_key: ClassVar[str | None] = "static_default_boundary"

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon:
        return default_bbox(latitude, longitude)


def create_bbox_str(latitude: float, longitude: float, delta: float = 0.005) -> str:
    return f"{longitude - delta},{latitude - delta},{longitude + delta},{latitude + delta}"


def create_bbox(latitude: float, longitude: float, delta: float = 0.001) -> BBox:
    return (longitude - delta, latitude - delta, longitude + delta, latitude + delta)


def default_bbox(latitude: float, longitude: float) -> Polygon:
    """Return the local no-network fallback boundary around a coordinate."""
    delta = DEFAULT_BBOX_DEGREES
    return Polygon.from_bbox((longitude - delta, latitude - delta, longitude + delta, latitude + delta))


def validate_bbox(bbox: BBox) -> None:
    """Raise ``ValueError`` if ``bbox`` isn't a sane, correctly-ordered box."""
    if len(bbox) != 4:
        raise ValueError(f"bbox must be (min_lon, min_lat, max_lon, max_lat), got {bbox!r}")
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (-180.0 <= min_lon < max_lon <= 180.0):
        raise ValueError(f"Invalid longitude range in bbox: {bbox!r}")
    if not (-90.0 <= min_lat < max_lat <= 90.0):
        raise ValueError(f"Invalid latitude range in bbox: {bbox!r}")


def bbox_to_polygon_geojson(bbox: BBox) -> dict:
    """Convert a bbox into a closed, counter-clockwise GeoJSON Polygon geometry."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat],
            ],
        ],
    }


def bbox_intersects(a: BBox, b: BBox) -> bool:
    """True if two bboxes overlap (touching edges count as overlap)."""
    a_min_lon, a_min_lat, a_max_lon, a_max_lat = a
    b_min_lon, b_min_lat, b_max_lon, b_max_lat = b
    return a_min_lon <= b_max_lon and a_max_lon >= b_min_lon and a_min_lat <= b_max_lat and a_max_lat >= b_min_lat


def _is_reasonable_default(polygon: Polygon) -> bool:
    return 0 < polygon.area <= MAX_DEFAULT_BOUNDARY_AREA_DEGREES


def _iter_positions(coords) -> Iterable[tuple[float, float]]:
    """Recursively walk a GeoJSON ``coordinates`` array, yielding (lon, lat) pairs."""
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        yield coords[0], coords[1]
        return
    for item in coords:
        yield from _iter_positions(item)


def geometry_bbox(geometry: dict) -> BBox | None:
    """Compute the bounding box of a GeoJSON geometry, without a shapely dependency."""
    lons: list[float] = []
    lats: list[float] = []
    for lon, lat in _iter_positions(geometry.get("coordinates", [])):
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def feature_intersects_bbox(feature: dict, bbox: BBox) -> bool:
    """True if a GeoJSON Feature's geometry overlaps ``bbox``.

    This is a cheap bounding-box test, not an exact polygon intersection --
    good enough for filtering large downloaded shards, not for precise
    spatial joins. Use shapely/GeoPandas downstream if you need exactness.
    """
    geometry = feature.get("geometry")
    if not geometry:
        return False
    feature_bbox = geometry_bbox(geometry)
    if feature_bbox is None:
        return False
    return bbox_intersects(feature_bbox, bbox)


def best_containing_polygon(features: list[dict], latitude: float, longitude: float) -> Polygon | None:
    point = Point(float(longitude), float(latitude), srid=4326)
    candidates: list[Polygon] = []
    for feature in features:
        polygon = _polygon_from_feature(feature)
        if polygon is None or not _is_reasonable_default(polygon):
            continue
        if polygon.contains(point) or polygon.touches(point):
            candidates.append(polygon)
    if not candidates:
        return None
    return min(candidates, key=lambda polygon: polygon.area)


def _polygon_from_feature(feature: dict) -> Polygon | None:
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if geometry is None and hasattr(feature, "__geo_interface__"):
        geometry = getattr(feature, "__geo_interface__", None)
    if hasattr(geometry, "__geo_interface__"):
        geometry = getattr(geometry, "__geo_interface__", None)
    if not geometry:
        return None

    try:
        geom = GEOSGeometry(json.dumps(geometry), srid=4326)
    except (TypeError, ValueError):
        return None
    return best_polygon_from_geometry(geom)


def best_polygon_from_geometry(geom: GEOSGeometry) -> Polygon | None:
    if geom.empty:
        return None
    if not geom.valid:
        geom = geom.buffer(0)
    if isinstance(geom, Polygon):
        return geom if geom.valid and not geom.empty else None
    if isinstance(geom, MultiPolygon):
        # Return the element itself, never re-wrapped in Polygon(...) - unlike
        # LineString/Point, Django's Polygon constructor has no "copy an
        # existing Polygon" overload, and passing one in raises (confirmed,
        # not hypothetical - see esri_rings_to_polygon's own docstring).
        polygons = [polygon for polygon in geom if isinstance(polygon, Polygon) and polygon.valid and not polygon.empty]
        return max(polygons, key=lambda polygon: polygon.area) if polygons else None
    return None


def _shoelace_signed_area(coords: list[tuple[float, float]]) -> float:
    """Standard shoelace signed area (x=lon, y=lat) - negative means clockwise winding."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in itertools.pairwise(coords)) / 2.0


def _close_ring(points: list) -> list[tuple[float, float]] | None:
    """Coerce a raw Esri ring (list of [lon, lat] pairs) into a closed coordinate list."""
    coords: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            coords.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            continue
    if len(coords) < 3:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords if len(coords) >= 4 else None


def esri_rings_to_polygon(geometry: dict | None) -> Polygon | MultiPolygon | None:
    """Convert an Esri ring-list polygon geometry (REData's ``parcel_geometry``/
    ``building_geometry`` shape - see ``schema.PropertyRecord.parcel_geometry``'s
    docstring in REData for why it's not plain GeoJSON) into a GEOS polygon.

    Esri's ring-winding convention is the opposite of GeoJSON's: a clockwise
    ring is an exterior shell, a counter-clockwise ring is a hole - and
    unlike GeoJSON, Esri doesn't guarantee a hole immediately follows its
    shell in the array, so each hole is assigned to whichever shell actually
    contains it (a point-in-polygon test), not just "the most recent shell".
    Multiple disjoint shells (a parcel/building made of separate pieces)
    become a MultiPolygon.

    Args:
        geometry: A dict of the shape ``{"format": "esri_rings", "rings": [...]}``,
            or None.

    Returns:
        A single ``Polygon``, a ``MultiPolygon`` when more than one exterior
        shell was found, or None when the geometry is missing, malformed, or
        has no usable exterior ring.
    """
    if not isinstance(geometry, dict) or geometry.get("format") != "esri_rings":
        return None
    rings = geometry.get("rings")
    if not isinstance(rings, list) or not rings:
        return None

    shell_coords: list[list[tuple[float, float]]] = []
    hole_coords: list[list[tuple[float, float]]] = []
    for ring in rings:
        if not isinstance(ring, list):
            continue
        coords = _close_ring(ring)
        if coords is None:
            continue
        (hole_coords if _shoelace_signed_area(coords) > 0 else shell_coords).append(coords)

    if not shell_coords:
        return None

    shell_polygons: list[Polygon] = []
    valid_shell_coords: list[list[tuple[float, float]]] = []
    for coords in shell_coords:
        try:
            polygon = Polygon(coords, srid=4326)
        except (ValueError, GEOSException):
            continue
        if polygon.valid and not polygon.empty:
            shell_polygons.append(polygon)
            valid_shell_coords.append(coords)

    if not shell_polygons:
        return None

    # Bucket each hole under whichever shell actually contains it.
    holes_by_shell: dict[int, list[list[tuple[float, float]]]] = {}
    for hole in hole_coords:
        point = Point(hole[0][0], hole[0][1], srid=4326)
        for idx, shell_polygon in enumerate(shell_polygons):
            if shell_polygon.contains(point):
                holes_by_shell.setdefault(idx, []).append(hole)
                break

    final_polygons: list[Polygon] = []
    for idx, coords in enumerate(valid_shell_coords):
        assigned_holes = holes_by_shell.get(idx)
        polygon = shell_polygons[idx]
        if assigned_holes:
            try:
                with_holes = Polygon(coords, *assigned_holes, srid=4326)
            except (ValueError, GEOSException):
                with_holes = None
            if with_holes is not None and with_holes.valid and not with_holes.empty:
                polygon = with_holes
        final_polygons.append(polygon)

    if len(final_polygons) == 1:
        return final_polygons[0]
    return MultiPolygon(final_polygons, srid=4326)
