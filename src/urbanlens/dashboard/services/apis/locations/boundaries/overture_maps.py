"""Gateway for Overture Maps' cloud-hosted GeoParquet themes.

Overture doesn't expose a request/response REST API either: each theme
(buildings, addresses, places, transportation, divisions, land use, ...) is
published as partitioned GeoParquet directly on S3 and Azure Blob Storage,
and clients read only the byte ranges they need via HTTP range requests --
"the storage endpoint is the API."

This gateway wraps Overture's own official Python client
(https://github.com/OvertureMaps/overturemaps-py), which handles resolving
the latest release via Overture's STAC catalog, bounding-box pushdown, and
returning a ready-to-use GeoPandas GeoDataFrame -- this project already
depends on GeoPandas elsewhere.

Because there's no per-call HTTP endpoint of ours to rate limit (the reads
happen inside pyarrow/S3 client internals, not through ``self.session``),
``service_key`` is intentionally left unset here -- this gateway opts out of
the ``Gateway`` rate limiter/call logging rather than pretending to
use it.

Install: `pip install overturemaps[geopandas]` (or `pip install geopandas`
separately, since it's a peer dependency, not a hard one, of overturemaps).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, ClassVar

from django.contrib.gis.geos import Point

from urbanlens.dashboard.services.apis.locations.base import (
    BOUNDARY_LOOKUP_BBOX_DEGREES,
    BBox,
    BoundaryProvider,
    _polygon_from_feature,
    best_containing_polygon,
    create_bbox,
    validate_bbox,
)

# Adjust this import to wherever Gateway/Gateway actually live.
from urbanlens.dashboard.services.gateway import Gateway

try:
    from overturemaps import geodataframe as _overture_geodataframe  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover
    _overture_geodataframe = None

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon


def _clean_value(value: Any) -> Any:
    """Turn Overture's pandas ``NaN``/blank-string "no value" markers into ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


@dataclass(slots=True, kw_only=True)
class OvertureMapsGateway(Gateway, BoundaryProvider):
    """Fetch Overture Maps theme data (buildings, addresses, places, ...) by bbox.

    Attributes:
        release: Pin a specific release tag (e.g. "2026-06-17.0"). Leave as
            None to always resolve the latest release via Overture's STAC
            catalog.
        connect_timeout: Optional S3 connection timeout, seconds.
        request_timeout: Optional S3 request timeout, seconds.
    """

    service_key: ClassVar[str | None] = None  # no HTTP endpoint of ours to rate-limit
    paid_service: ClassVar[bool] = False
    boundary_kind: ClassVar[str] = "building"

    release: str | None = None
    connect_timeout: int | None = None
    request_timeout: int | None = None
    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES

    def __post_init__(self) -> None:
        # NOTE: zero-arg super() breaks here because @dataclass(slots=True)
        # rebuilds the class object, invalidating the implicit __class__ cell.
        # Call the parent explicitly instead.
        Gateway.__post_init__(self)

    def _fetch(self, overture_type: str, bbox: BBox | None):
        if bbox is not None:
            validate_bbox(bbox)
        if _overture_geodataframe is None:
            raise ImportError(
                "OvertureMapsGateway requires the 'overturemaps' package: `pip install overturemaps[geopandas]`.",
            )
        return _overture_geodataframe(
            overture_type,
            bbox=bbox,
            release=self.release,
            connect_timeout=self.connect_timeout,
            request_timeout=self.request_timeout,
        )

    # -- Building / property boundary relevant themes ------------------------

    def get_buildings(self, bbox: BBox):
        """Building footprint polygons (theme=buildings, type=building)."""
        return self._fetch("building", bbox)

    def get_building_parts(self, bbox: BBox):
        """Sub-building parts -- individual wings/sections of a footprint."""
        return self._fetch("building_part", bbox)

    def get_divisions(self, bbox: BBox):
        """Administrative division boundaries (country/state/county/etc)."""
        return self._fetch("division_area", bbox)

    # -- Everything else Overture publishes -----------------------------------

    def get_addresses(self, bbox: BBox):
        return self._fetch("address", bbox)

    def get_places(self, bbox: BBox):
        """POIs -- useful for attaching names/categories to building footprints."""
        return self._fetch("place", bbox)

    def get_land_use(self, bbox: BBox):
        return self._fetch("land_use", bbox)

    def get_land(self, bbox: BBox):
        """Natural land cover/physical land features (not administrative)."""
        return self._fetch("land", bbox)

    def get_land_cover(self, bbox: BBox):
        return self._fetch("land_cover", bbox)

    def get_water(self, bbox: BBox):
        return self._fetch("water", bbox)

    def get_segments(self, bbox: BBox):
        """Transportation network segments (roads, paths, rail, ...)."""
        return self._fetch("segment", bbox)

    def get_connectors(self, bbox: BBox):
        """Transportation network topology nodes connecting segments."""
        return self._fetch("connector", bbox)

    def get_infrastructure(self, bbox: BBox):
        return self._fetch("infrastructure", bbox)

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        return best_containing_polygon(
            _features_from_geodataframe(self.get_buildings(create_bbox(latitude, longitude, self.bbox_delta))),
            latitude,
            longitude,
        )

    def get_building_attributes(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Return the pinned building's physical attributes from Overture's Buildings theme.

        "Real estate" context Overture actually publishes (Overture has no
        year-built field): the building class/subtype, height, floor count,
        and roof construction, plus its primary name when Overture has one.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Dict with ``class_``, ``subtype``, ``height_m``, ``num_floors``,
            ``roof_shape``, ``roof_material``, ``primary_name`` (each ``None``
            when Overture has no value), or None when no building footprint
            contains the point.
        """
        point = Point(float(longitude), float(latitude), srid=4326)
        best_area: float | None = None
        best_properties: dict[str, Any] | None = None
        for feature in _features_from_geodataframe(self.get_buildings(create_bbox(latitude, longitude, self.bbox_delta))):
            polygon = _polygon_from_feature(feature)
            if polygon is None or not (polygon.contains(point) or polygon.touches(point)):
                continue
            if best_area is None or polygon.area < best_area:
                best_area = polygon.area
                best_properties = feature.get("properties") or {}

        if best_properties is None:
            return None

        names = _clean_value(best_properties.get("names"))
        primary_name = names.get("primary") if isinstance(names, dict) else None
        return {
            "class_": _clean_value(best_properties.get("class")),
            "subtype": _clean_value(best_properties.get("subtype")),
            "height_m": _clean_value(best_properties.get("height")),
            "num_floors": _clean_value(best_properties.get("num_floors")),
            "roof_shape": _clean_value(best_properties.get("roof_shape")),
            "roof_material": _clean_value(best_properties.get("roof_material")),
            "primary_name": _clean_value(primary_name),
        }


def _features_from_geodataframe(frame) -> list[dict]:
    if hasattr(frame, "iterfeatures"):
        return list(frame.iterfeatures())
    if isinstance(frame, list):
        return frame
    return []
