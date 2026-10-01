"""Gateway for Overture Maps' cloud-hosted GeoParquet themes."""

from __future__ import annotations

from dataclasses import dataclass
import io
import logging
import math
import time
from typing import Any, ClassVar
from urllib.request import urlopen

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
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRateLimitedError, GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, _finalize_call, _reserve_call
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

logger = logging.getLogger(__name__)

#: Seconds to stop calling Overture after its STAC index refuses us.
#: Per process, deliberately: each prefork child keeps its own, so a pool of four probes at most four
#: times a window instead of once per task.
_STAC_COOLDOWN_SECONDS = 120.0

#: When the circuit re-closes. Module-level, one per worker child.
_stac_unavailable_until = 0.0

#: Seconds the STAC lookup gets before it counts as unavailable, per socket operation and overall.
_STAC_LOOKUP_TIMEOUT_SECONDS = 15.0

_STAC_ROOT = "https://stac.overturemaps.org"

#: Seconds a resolved "latest" release is reused before the catalog is asked again.
_LATEST_RELEASE_TTL_SECONDS = 3600.0

#: ``(release, expires_at)`` from the last catalog answer, per process.
_latest_release_cache: tuple[str, float] | None = None


def _latest_release() -> str | None:
    """The newest Overture release, or None when the catalog cannot say within the deadline."""
    global _latest_release_cache  # noqa: PLW0603

    now = time.monotonic()
    if _latest_release_cache is not None and now < _latest_release_cache[1]:
        return _latest_release_cache[0]
    if _overture_core is None:  # pragma: no cover
        return None
    try:
        release = call_with_deadline(_overture_core.get_latest_release, timeout=_STAC_LOOKUP_TIMEOUT_SECONDS, default=None, name="overture-latest-release")
    except (OSError, ValueError, KeyError):
        return None
    if release:
        _latest_release_cache = (release, now + _LATEST_RELEASE_TTL_SECONDS)
    return release or None


#: Each release's index as ``(s3_key, (xmin, ymin, xmax, ymax))`` pairs. A published index never changes.
_stac_index_cache: dict[str, list[tuple[str, tuple[float, float, float, float]]]] = {}


def _index_entries(release: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every data file the release's STAC index lists, with its extent.

    Raises:
        OSError: The index could not be fetched.
        pyarrow.ArrowException: The index could not be parsed.
    """
    if (cached := _stac_index_cache.get(release)) is not None:
        return cached
    import pyarrow.parquet as pq

    with urlopen(f"{_STAC_ROOT}/{release}/collections.parquet", timeout=_STAC_LOOKUP_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https origin
        table = pq.read_table(io.BytesIO(response.read()), columns=["assets", "bbox"])
    entries = []
    for asset, extent in zip(table.column("assets").to_pylist(), table.column("bbox").to_pylist(), strict=True):
        href = (((asset or {}).get("aws") or {}).get("alternate") or {}).get("s3", {}).get("href") or ""
        if href.startswith("s3://") and extent:
            entries.append((href.removeprefix("s3://"), (extent["xmin"], extent["ymin"], extent["xmax"], extent["ymax"])))
    _stac_index_cache[release] = entries
    return entries


def _intersecting_files(theme: str, overture_type: str, bbox: tuple[float, float, float, float], release: str) -> list[str] | None:
    """S3 keys of the release's ``overture_type`` files whose extent meets ``bbox``, or None when the index is unreadable.

    Matched on the key's ``theme=/type=`` partition rather than the index's ``collection`` column, which
    release 2026-08-19.0 publishes null on every row - the library filters on it and finds nothing anywhere.
    """
    import pyarrow as pa

    try:
        entries = _index_entries(release)
    except (OSError, ValueError, pa.ArrowException):
        logger.warning("Overture STAC index for release %s is unreadable", release, exc_info=True)
        return None
    partition = f"/theme={theme}/type={overture_type}/"
    xmin, ymin, xmax, ymax = bbox
    return [key for key, (fxmin, fymin, fxmax, fymax) in entries if partition in key and fxmin < xmax and fxmax > xmin and fymin < ymax and fymax > ymin]


def _read_files(files: list[str], bbox: tuple[float, float, float, float], *, connect_timeout: int | None, request_timeout: int | None) -> Any:
    """Read ``files`` from Overture's bucket, keeping the rows that meet ``bbox``."""
    from geopandas import GeoDataFrame
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import pyarrow.fs as pafs

    if not files:
        return GeoDataFrame(geometry=[], crs="EPSG:4326")
    xmin, ymin, xmax, ymax = bbox
    row_filter = (pc.field("bbox", "xmin") < xmax) & (pc.field("bbox", "xmax") > xmin) & (pc.field("bbox", "ymin") < ymax) & (pc.field("bbox", "ymax") > ymin)
    dataset = ds.dataset(files, filesystem=pafs.S3FileSystem(anonymous=True, region="us-west-2", connect_timeout=connect_timeout, request_timeout=request_timeout))
    reader = _overture_core._record_batch_reader_from_dataset(dataset, filter_expr=row_filter)  # noqa: SLF001
    if reader is None:
        raise GatewayRequestError(f"Overture could not read {len(files)} file(s) for this bbox")
    return GeoDataFrame.from_arrow(reader)


_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres.

    Args:
        lat1: First latitude in degrees.
        lon1: First longitude in degrees.
        lat2: Second latitude in degrees.
        lon2: Second longitude in degrees.

    Returns:
        Distance in metres."""
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
        release: Leave as None to always resolve the latest release via Overture's STAC catalog.
        connect_timeout: S3 connection timeout, seconds.
        request_timeout: S3 request timeout, seconds."""

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
        entry_pk = self._reserve_call_budget(overture_type)
        started = time.monotonic()
        try:
            release = self._resolve_release()
            files = self._narrowed_files(overture_type, bbox, release) if bbox is not None else None
            if files is not None and bbox is not None:
                result = _read_files(files, _overture_core._coerce_bbox(bbox).as_tuple(), connect_timeout=self.connect_timeout, request_timeout=self.request_timeout)  # noqa: SLF001
            else:
                result = _overture_geodataframe(overture_type, bbox=None, release=release, connect_timeout=self.connect_timeout, request_timeout=self.request_timeout)
        except Exception:
            _finalize_call(entry_pk, success=False, response_ms=int((time.monotonic() - started) * 1000))
            raise
        _finalize_call(entry_pk, success=True, response_ms=int((time.monotonic() - started) * 1000))
        return result

    def _reserve_call_budget(self, overture_type: str) -> int:
        """Refuse the call before it reaches Overture, if this service's own budget is spent.

        `_fetch` reads GeoParquet straight from S3 via `pyarrow`/`geopandas`, bypassing
        `self.session` entirely - the one place `Gateway` normally wires up rate limiting (see
        `Gateway.__post_init__`). Without this, nothing bounds how often enrichment reaches
        Overture in the first place, including the STAC index lookup in `_narrowed_files` -
        the thing that actually earns the 429 that disables narrowing. See P110.

        Goes through ``_reserve_call``/``_finalize_call`` (the same pair `_RateLimitedSession` uses)
        rather than the simpler `check_rate_limit`+`log_api_call` pattern the AI services use for
        their own `self.session` bypass: those are called once per task, but Overture is reached from
        per-pin enrichment fan-out - the exact concurrent-burst scenario P110 is about - and a plain
        check-then-log gap there would let concurrent callers all pass the check before any of them
        logs, defeating the budget precisely when it matters most.

        Args:
            overture_type: The Overture type being fetched, recorded on the reservation's log entry.

        Returns:
            The pk of the reserved ``ApiCallLog`` row - pass it to ``_finalize_call`` once the read
            completes.

        Raises:
            GatewayRateLimitedError: This service is administratively disabled, this deployment's
                outbound calls are off (see ``outbound_calls_permitted``), or this service's own
                request budget (``SERVICE_REGISTRY["overture_maps"]``) is exhausted for the
                current window.
        """
        service = type(self).service_key
        if service is None:  # pragma: no cover - ServiceMeta always derives one from the class name
            raise RuntimeError("OvertureMapsGateway has no service_key; ServiceMeta should have derived one")
        try:
            return _reserve_call(service, endpoint=overture_type)
        except RequestCancelledError as exc:
            raise GatewayRateLimitedError(str(exc)) from exc

    def _resolve_release(self) -> str:
        """The pinned release, else the latest one.

        Resolved here rather than left to the library: its read resolves "latest" itself, but its
        STAC index lookup does not, and asks for ``/None/collections.parquet``.

        Returns:
            A concrete release version.

        Raises:
            GatewayRateLimitedError: No release is pinned and the catalog did not answer.
        """
        global _stac_unavailable_until  # noqa: PLW0603

        if self.release:
            return self.release
        now = time.monotonic()
        if now < _stac_unavailable_until:
            raise GatewayRateLimitedError(
                f"Overture's STAC catalog refused us within the last {_STAC_COOLDOWN_SECONDS:.0f}s; not looking up buildings.",
            )
        release = _latest_release()
        if release is None:
            _stac_unavailable_until = now + _STAC_COOLDOWN_SECONDS
            raise GatewayRateLimitedError("Overture's STAC catalog did not name a latest release, so there is no index to narrow a lookup with.")
        return release

    def _narrowed_files(self, overture_type: str, bbox: BBox, release: str) -> list[str]:
        """The files the STAC index says intersect ``bbox``; refuse rather than read the whole theme.

        Without the index a lookup, however small, opens every partition file of the global theme.

        Args:
            overture_type: The Overture type being fetched.
            bbox: The bounding box being looked up.
            release: The concrete release whose index is consulted.

        Returns:
            S3 keys, possibly none: "no files here" is an answer.

        Raises:
            GatewayRateLimitedError: The index is unavailable, now or recently.
        """
        global _stac_unavailable_until  # noqa: PLW0603

        now = time.monotonic()
        if now < _stac_unavailable_until:
            raise GatewayRateLimitedError(
                f"Overture's STAC index refused us within the last {_STAC_COOLDOWN_SECONDS:.0f}s; not looking up buildings, because without it the read is the whole theme.",
            )

        theme = _overture_core.type_theme_map[overture_type]
        extent = _overture_core._coerce_bbox(bbox).as_tuple()  # noqa: SLF001
        # A lookup too slow to answer is as useless as one that refuses: both stop the read.
        resolved = call_with_deadline(
            lambda: _intersecting_files(theme, overture_type, extent, release),
            timeout=_STAC_LOOKUP_TIMEOUT_SECONDS,
            default=None,
            name="overture-stac-index",
        )
        if resolved is None:
            _stac_unavailable_until = now + _STAC_COOLDOWN_SECONDS
            raise GatewayRateLimitedError(
                "Overture's STAC index is unavailable, so a lookup would read the entire theme rather than the files intersecting this bbox. Refusing instead; see P110.",
            )
        return resolved

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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Dict with ``class_``, ``subtype``, ``height_m``, ``num_floors``, ``roof_shape``, ``roof_material``, ``primary_name`` (each ``None`` when Overture has no value), or None when no building footprint contains the point.
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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_m: Search radius in meters.
            limit: Maximum number of places to return, nearest first.

        Returns:
            Dicts with ``name``, ``category``, ``confidence``, ``operating_status``, ``distance_m``, nearest first; empty when nothing named is within range.
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
