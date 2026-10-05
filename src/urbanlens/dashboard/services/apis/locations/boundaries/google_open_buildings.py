"""Gateway for Google's Open Buildings dataset (V3)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import gzip
import io
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import DEFAULT_CACHE_ALIAS

from urbanlens.dashboard.services.apis.locations.base import BOUNDARY_LOOKUP_BBOX_DEGREES, BBox, BoundaryProvider, best_containing_polygon, create_bbox, validate_bbox
from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_or_skip
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError, read_capped

try:
    import s2sphere
except ImportError:  # pragma: no cover
    s2sphere = None

try:
    from shapely import wkt as shapely_wkt
    from shapely.geometry import mapping as shapely_mapping
except ImportError:  # pragma: no cover
    shapely_wkt = None
    shapely_mapping = None

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.contrib.gis.geos import Polygon

POLYGONS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip"
POINTS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
S2_COVERING_LEVEL = 4
#: Shards run to gigabytes where the dataset is dense (Lagos's polygons are 1.9 GB compressed); one past this is skipped.
MAX_SHARD_BYTES = 64 * 1024 * 1024
#: A shard that is missing or too large stays so for as long as the dataset's release does.
_UNUSABLE_SHARD_SECONDS = 30 * 24 * 60 * 60

logger = logging.getLogger(__name__)


def _s2_tokens_for_bbox(bbox: BBox) -> list[str]:
    if s2sphere is None:
        raise ImportError(
            "GoogleOpenBuildingsGateway requires the 's2sphere' package to compute S2 cell coverings. Install with `pip install s2sphere`.",
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    region = s2sphere.LatLngRect(
        s2sphere.LatLng.from_degrees(min_lat, min_lon),
        s2sphere.LatLng.from_degrees(max_lat, max_lon),
    )
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = S2_COVERING_LEVEL
    coverer.max_level = S2_COVERING_LEVEL
    return [cell_id.to_token() for cell_id in coverer.get_covering(region)]


@dataclass(slots=True, kw_only=True)
class GoogleOpenBuildingsGateway(Gateway, BoundaryProvider):
    """Fetch building polygons/points from Google's Open Buildings V3 dataset.

    Attributes:
        min_confidence: Drop rows below this model confidence score (dataset range is roughly 0.65-1.0)."""

    service_key: ClassVar[str | None] = "google_open_buildings"
    paid_service: ClassVar[bool] = False
    boundary_kind: ClassVar[str] = "building"

    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    min_confidence: float = 0.0

    def get_buildings(self, bbox: BBox, *, as_geojson: bool = True) -> list[dict]:
        """Building footprint polygons overlapping ``bbox``.
        Pass ``as_geojson=False`` to get raw dicts with a ``geometry_wkt`` string instead, if you'd rather avoid the shapely dependency."""
        return self._download_shards(bbox, POLYGONS_BASE_URL, has_geometry=True, as_geojson=as_geojson)

    def get_building_points(self, bbox: BBox) -> list[dict]:
        """Building centroid points overlapping ``bbox`` (smaller/faster than polygons)."""
        return self._download_shards(bbox, POINTS_BASE_URL, has_geometry=False, as_geojson=False)

    def _download_shards(self, bbox: BBox, base_url: str, *, has_geometry: bool, as_geojson: bool) -> list[dict]:
        validate_bbox(bbox)
        results: list[dict] = []
        for token in _s2_tokens_for_bbox(bbox):
            shard = self._read_shard(f"{base_url}/{token}_buildings.csv.gz")
            if shard is None:
                continue
            with gzip.GzipFile(fileobj=io.BytesIO(shard)) as unzipped, io.TextIOWrapper(unzipped, encoding="utf-8", newline="") as text:
                results.extend(self._rows_within(csv.DictReader(text), bbox, has_geometry=has_geometry, as_geojson=as_geojson))
        return results

    def _read_shard(self, url: str) -> bytes | None:
        """A shard's compressed body, or None when it is missing or past ``MAX_SHARD_BYTES``; either is remembered.

        Runs inside the boundary panel-fetch Celery task, whose soft time limit is the real budget; the (connect, read)
        timeout keeps a dead connection from spending it.
        """
        unusable_key = f"google-open-buildings:unusable:{url}"
        if get_or_none(unusable_key, label="google open buildings shard", alias=DEFAULT_CACHE_ALIAS):
            return None
        response = self.session.get(url, timeout=(5, 60), stream=True)
        try:
            if response.status_code == 404:
                reason = "missing"
            else:
                response.raise_for_status()
                length = response.headers.get("Content-Length", "")
                reason = "too large" if length.isdigit() and int(length) > MAX_SHARD_BYTES else ""
            if not reason:
                try:
                    return read_capped(response, max_bytes=MAX_SHARD_BYTES, what="An Open Buildings shard")
                except GatewayRequestError:
                    reason = "too large"
        finally:
            response.close()
        if reason == "too large":
            logger.info("Skipping the Open Buildings shard %s: it is larger than %s bytes", url, MAX_SHARD_BYTES)
        set_or_skip(unusable_key, reason, _UNUSABLE_SHARD_SECONDS, label="google open buildings shard", alias=DEFAULT_CACHE_ALIAS)
        return None

    def _rows_within(self, rows: csv.DictReader[str], bbox: BBox, *, has_geometry: bool, as_geojson: bool) -> Iterator[dict[str, Any]]:
        min_lon, min_lat, max_lon, max_lat = bbox
        for row in rows:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                continue
            if float(row.get("confidence") or 0.0) < self.min_confidence:
                continue
            yield self._row_to_output(row, has_geometry=has_geometry, as_geojson=as_geojson)

    @staticmethod
    def _row_to_output(row: dict[str, str], *, has_geometry: bool, as_geojson: bool) -> dict[str, Any]:
        properties = {
            "area_in_meters": float(row["area_in_meters"]) if row.get("area_in_meters") else None,
            "confidence": float(row["confidence"]) if row.get("confidence") else None,
            "full_plus_code": row.get("full_plus_code"),
        }
        if not (has_geometry and as_geojson):
            return {
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                **properties,
                **({"geometry_wkt": row.get("geometry")} if has_geometry else {}),
            }
        if shapely_wkt is None or shapely_mapping is None:
            raise ImportError(
                "Converting Google Open Buildings polygons to GeoJSON requires shapely (`pip install shapely`), or call get_buildings(..., as_geojson=False).",
            )
        return {
            "type": "Feature",
            "geometry": shapely_mapping(shapely_wkt.loads(row["geometry"])),
            "properties": properties,
        }

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        return best_containing_polygon(
            self.get_buildings(create_bbox(latitude, longitude, self.bbox_delta)),
            latitude,
            longitude,
        )
