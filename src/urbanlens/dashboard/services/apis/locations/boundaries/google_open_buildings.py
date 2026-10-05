"""Gateway for Google's Open Buildings dataset (v3), read from Google's own bucket one S2 cell's shard at a time.

v3 (2023) is the current release of the 2D polygons and centroid points. The 2.5D Temporal dataset is published only
as Earth Engine rasters, so it is not a source here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import cache
import gzip
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BOUNDARY_LOOKUP_BBOX_DEGREES, BBox, BoundaryProvider, best_containing_polygon, create_bbox, validate_bbox
from urbanlens.dashboard.services.apis.locations.boundaries.shards import read_shard
from urbanlens.dashboard.services.core.gateway import Gateway

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

#: v3 is also published as level-4 shards, but 200 of those 333 are past :data:`MAX_SHARD_BYTES` (median 138 MiB),
#: against 626 of these 3,330 (median 8.8 MiB). A level-6 shard has no header row.
POLYGONS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_6_gzip_no_header"
POINTS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_6_gzip_no_header"
S2_SHARD_LEVEL = 6
#: The level-4 shards' header row, which the level-6 shards share without stating.
POLYGON_COLUMNS = ("latitude", "longitude", "area_in_meters", "confidence", "geometry", "full_plus_code")
POINT_COLUMNS = ("latitude", "longitude", "area_in_meters", "confidence", "full_plus_code")
#: Shards run to gigabytes where the dataset is dense (Luang Prabang's level-4 shard is 3.5 GB compressed, its
#: level-6 one 37 MiB); one past this is skipped.
MAX_SHARD_BYTES = 64 * 1024 * 1024

#: Every level-4 S2 cell v3 has shards under, one token a line: Africa, South and Southeast Asia, Latin America and
#: the Caribbean. Each of the 3,330 level-6 shards lies in one of them. Listed from ``v3/polygons_s2_level_4_gzip/`` on
#: 2026-10-05; the release has not changed since 2023-06-23.
COVERED_CELLS_FILE = Path(__file__).with_name("data") / "open_buildings_v3_level_4_cells.txt"

logger = logging.getLogger(__name__)


@cache
def covered_level_4_cells() -> frozenset[str]:
    """The level-4 cells the dataset has shards under (:data:`COVERED_CELLS_FILE`)."""
    return frozenset(COVERED_CELLS_FILE.read_text(encoding="utf-8").split())


def _shard_tokens_for_bbox(bbox: BBox) -> list[str]:
    """The level-6 cells overlapping ``bbox`` that the dataset covers.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)``.

    Returns:
        S2 cell tokens, one per shard to read.

    Raises:
        ImportError: ``s2sphere`` is not installed."""
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
    coverer.min_level = S2_SHARD_LEVEL
    coverer.max_level = S2_SHARD_LEVEL
    return [cell_id.to_token() for cell_id in coverer.get_covering(region) if cell_id.parent(4).to_token() in covered_level_4_cells()]


@dataclass(slots=True, kw_only=True)
class GoogleOpenBuildingsGateway(Gateway, BoundaryProvider):
    """Fetch building polygons/points from Google's Open Buildings v3 dataset.

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
        return self._download_shards(bbox, POLYGONS_BASE_URL, POLYGON_COLUMNS, as_geojson=as_geojson)

    def get_building_points(self, bbox: BBox) -> list[dict]:
        """Building centroid points overlapping ``bbox`` (smaller/faster than polygons)."""
        return self._download_shards(bbox, POINTS_BASE_URL, POINT_COLUMNS, as_geojson=False)

    def _download_shards(self, bbox: BBox, base_url: str, columns: tuple[str, ...], *, as_geojson: bool) -> list[dict]:
        validate_bbox(bbox)
        has_geometry = "geometry" in columns
        results: list[dict] = []
        for token in _shard_tokens_for_bbox(bbox):
            shard = read_shard(self.session, f"{base_url}/{token}_buildings.csv.gz", max_bytes=MAX_SHARD_BYTES, what="An Open Buildings shard")
            if shard is None:
                continue
            with gzip.GzipFile(fileobj=io.BytesIO(shard)) as unzipped, io.TextIOWrapper(unzipped, encoding="utf-8", newline="") as text:
                rows = csv.DictReader(text, fieldnames=columns)
                results.extend(self._rows_within(rows, bbox, has_geometry=has_geometry, as_geojson=as_geojson))
        return results

    def _rows_within(self, rows: csv.DictReader[str], bbox: BBox, *, has_geometry: bool, as_geojson: bool) -> Iterator[dict[str, Any]]:
        min_lon, min_lat, max_lon, max_lat = bbox
        for row in rows:
            # A short row leaves its missing columns None, and a long one puts the rest under None.
            if None in row or None in row.values():
                continue
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
