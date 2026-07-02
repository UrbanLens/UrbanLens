"""Shared data types for the satellite imagery and street-view carousels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Protocol

from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.gateway import Gateway

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

BBox = tuple[float, float, float, float]


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
    def _generate_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = -1) -> Generator[SatelliteSlide]:
        ...
    
    def get_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = 5) -> list[SatelliteSlide]:
        cache_key = make_cache_key(f"satellite_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        if slides := cache.get(cache_key):
            return slides

        slides = []
        for slide in self._generate_satellite_slides(latitude, longitude, zoom=zoom, width=width, height=height):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        cache.set(cache_key, slides, 24 * 3600)
        return slides


class StreetViewProvider(Gateway, ABC):
    @abstractmethod
    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        ...

    def get_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> list[StreetViewSlide]:
        cache_key = make_cache_key(f"street_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        if slides := cache.get(cache_key):
            return slides

        slides = []
        for slide in self._generate_street_view_slides(latitude, longitude, radius=radius):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        return slides


def create_bbox(latitude: float, longitude: float, delta: float = 0.005) -> str:
    return f"{longitude - delta},{latitude - delta},{longitude + delta},{latitude + delta}"


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
    return (
        a_min_lon <= b_max_lon
        and a_max_lon >= b_min_lon
        and a_min_lat <= b_max_lat
        and a_max_lat >= b_min_lat
    )


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

