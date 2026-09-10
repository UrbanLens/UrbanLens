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
import time
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
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRateLimitedError
from urbanlens.dashboard.services.core.timeout_utils import call_with_deadline

try:
    from overturemaps import geodataframe as _overture_geodataframe  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover
    _overture_geodataframe = None

try:
    from overturemaps import core as _overture_core  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover
    _overture_core = None

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon

#: Seconds to stop calling Overture after its STAC index refuses us.
#:
#: Per process, deliberately: each prefork child keeps its own, so a pool of
#: four probes at most four times a window instead of once per task. In-process
#: rather than in the shared cache so this still works when Valkey is down,
#: which is exactly when a lookup storm is least welcome.
_STAC_COOLDOWN_SECONDS = 120.0

#: When the circuit re-closes. Module-level, one per worker child.
_stac_unavailable_until = 0.0

#: Seconds the STAC lookup gets before it counts as unavailable.
#:
#: It needs one because the library does not have one: `_get_files_from_stac`
#: calls `urlopen(stac_url)` with no timeout at all, so a stalled connection
#: blocks its thread forever. On the request path (the pin-detail panels reach
#: this too) that is every worker thread in turn, and the process stops
#: answering while using no CPU - observed exactly that way before this bound
#: existed. The index is small, so this is generous.
_STAC_LOOKUP_TIMEOUT_SECONDS = 15.0


_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres.

    Args:
        lat1: First latitude in degrees.
        lon1: First longitude in degrees.
        lat2: Second latitude in degrees.
        lon2: Second longitude in degrees.

    Returns:
        Distance in metres.
    """
    from urbanlens.dashboard.services.geo.distance import haversine_meters

    return haversine_meters(lat1, lon1, lat2, lon2)


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
        connect_timeout: S3 connection timeout, seconds. Defaults to a bound
            rather than None (unbounded) - a slow/stalled S3 read otherwise
            blocks the Celery worker running it well past the panel's own
            soft/hard time limits, since pyarrow's read isn't itself
            interruptible the way a plain requests call is. Pass None
            explicitly to opt back into no timeout.
        request_timeout: S3 request timeout, seconds. Same reasoning as
            connect_timeout, just a larger bound since a range-read over a
            GeoParquet shard can legitimately take longer than a bare TCP
            connect.
    """

    service_key: ClassVar[str | None] = None  # no HTTP endpoint of ours to rate-limit
    paid_service: ClassVar[bool] = False
    boundary_kind: ClassVar[str] = "building"

    release: str | None = None
    connect_timeout: int | None = 10
    request_timeout: int | None = 30
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
        if bbox is not None:
            self._require_narrowing(overture_type, bbox)
        return _overture_geodataframe(
            overture_type,
            bbox=bbox,
            release=self.release,
            connect_timeout=self.connect_timeout,
            request_timeout=self.request_timeout,
            # overturemaps-py defaults this to False, which skips the small
            # STAC-geoparquet index that resolves a bbox to the handful of S3
            # files that actually intersect it - without it, every lookup
            # (however small the bbox) opens a pyarrow dataset over the
            # *entire* global theme (hundreds of multi-gigabyte partition
            # files per theme) and depends on filter pushdown alone to prune
            # it while scanning. Verified against the 2026-08-19.0 release: a
            # ~111m bbox resolves to 1 intersecting file via STAC vs. 512
            # files in the unfiltered dataset. This was driving Celery worker
            # RSS into the multiple-gigabytes range per call and triggering
            # the kernel OOM killer - see docs/PROBLEMS.md.
            stac=True,
        )

    def _require_narrowing(self, overture_type: str, bbox: BBox) -> None:
        """Refuse the lookup unless the STAC index can narrow it first.

        `stac=True` below is a request, not a guarantee. `overturemaps.core`
        catches every exception from the index lookup, prints it, and returns
        ``None``; the caller then opens the theme's whole path instead of the
        intersecting partitions. So being rate-limited by Overture silently
        converts a one-file read into a scan of the planet, which is the OOM
        this gateway's `stac=True` was added to prevent (P110, and the entry it
        recurs from). Nothing in the library's API can express "narrow it or
        do not do it at all", so this asks first and refuses on its own.

        A refusal opens a short circuit. Without one, every queued enrichment
        task would keep probing an index that is refusing us, which is the loop
        that earned the rate limit - the breaker turns a self-amplifying failure
        into a self-limiting one.

        The cost is one extra read of the (small) index on the healthy path,
        because the library re-resolves it and will not accept a file list. That
        is worth paying to never scan the theme, and it goes away if
        `overturemaps` ever grows a strict mode or takes the resolved files.

        Args:
            overture_type: The Overture type being fetched.
            bbox: The bounding box being looked up.

        The lookup is given its own deadline because the library gives it none -
        `_get_files_from_stac` calls `urlopen` with no timeout, so a stalled
        connection parks the calling thread indefinitely. This is reached from
        the request path as well as from tasks.

        Raises:
            GatewayRateLimitedError: The index is unavailable, now or recently.
        """
        global _stac_unavailable_until  # noqa: PLW0603

        if _overture_core is None:  # pragma: no cover - import guard above covers the real case
            return

        now = time.monotonic()
        if now < _stac_unavailable_until:
            raise GatewayRateLimitedError(
                f"Overture's STAC index refused us within the last {_STAC_COOLDOWN_SECONDS:.0f}s; not looking up buildings, because without it the read is the whole theme.",
            )

        theme = _overture_core.type_theme_map[overture_type]
        coerced = _overture_core._coerce_bbox(bbox)  # noqa: SLF001
        # `default=None` deliberately joins the timeout to the failure path: a
        # lookup too slow to answer is as useless as one that refuses, and both
        # should stop the read rather than let it widen.
        resolved = call_with_deadline(
            lambda: _overture_core._get_files_from_stac(theme, overture_type, coerced, self.release),  # noqa: SLF001
            timeout=_STAC_LOOKUP_TIMEOUT_SECONDS,
            default=None,
            name="overture-stac-index",
        )
        if resolved is None:
            _stac_unavailable_until = now + _STAC_COOLDOWN_SECONDS
            raise GatewayRateLimitedError(
                "Overture's STAC index is unavailable, so a lookup would read the entire theme rather than the files intersecting this bbox. Refusing instead; see P110.",
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

    def get_nearby_places(self, latitude: float, longitude: float, *, radius_m: float = 150.0, limit: int = 5) -> list[dict[str, Any]]:
        """Return named points of interest near a coordinate from Overture's Places theme.

        Same free S3/parquet source already used for building footprints -
        surfaces what's actually nearby (shops, landmarks, defunct
        businesses), including an ``operating_status`` flag Overture derives
        from crowd signals, which is useful "is this still open" context.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_m: Search radius in meters.
            limit: Maximum number of places to return, nearest first.

        Returns:
            Dicts with ``name``, ``category``, ``confidence``,
            ``operating_status``, ``distance_m``, nearest first; empty when
            nothing named is within range.
        """
        candidates: list[dict[str, Any]] = []
        for feature in _features_from_geodataframe(self.get_places(create_bbox(latitude, longitude, self.bbox_delta))):
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or (None, None)
            place_lon, place_lat = coordinates[0], coordinates[1]
            if place_lon is None or place_lat is None:
                continue

            properties = feature.get("properties") or {}
            names = _clean_value(properties.get("names"))
            primary_name = names.get("primary") if isinstance(names, dict) else None
            if not primary_name:
                continue  # unnamed POIs aren't useful location context

            distance_m = _haversine_m(latitude, longitude, float(place_lat), float(place_lon))
            if distance_m > radius_m:
                continue

            categories = _clean_value(properties.get("categories"))
            primary_category = categories.get("primary") if isinstance(categories, dict) else None
            candidates.append(
                {
                    "name": primary_name,
                    "category": _clean_value(primary_category),
                    "confidence": _clean_value(properties.get("confidence")),
                    "operating_status": _clean_value(properties.get("operating_status")),
                    "distance_m": round(distance_m, 1),
                }
            )

        candidates.sort(key=lambda place: place["distance_m"])
        return candidates[: max(1, limit)]


def _features_from_geodataframe(frame) -> list[dict]:
    if hasattr(frame, "iterfeatures"):
        return list(frame.iterfeatures())
    if isinstance(frame, list):
        return frame
    return []
