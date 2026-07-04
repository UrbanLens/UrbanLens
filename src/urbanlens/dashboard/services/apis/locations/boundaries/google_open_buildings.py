"""Gateway for Google's Open Buildings dataset (V3).

Like Microsoft's dataset, this is static, sharded, downloadable data rather
than a query API -- Google publishes building polygons and centroid points
as gzip-compressed CSVs, one shard per S2 *level-4* cell, on public Google
Cloud Storage: https://sites.research.google/gr/open-buildings/

This gateway computes which S2 level-4 cells cover a bounding box (using
Google's own S2 geometry library via the ``s2sphere`` package), downloads
just those shards over HTTPS through ``self.session``, and filters rows to
the exact bounding box.

Coverage: mostly Africa, South Asia, and South-East Asia -- check Google's
coverage map before assuming a region is included.

Requires the optional dependency ``s2sphere`` (``pip install s2sphere``) to
compute S2 cell coverings, and ``shapely`` to turn WKT polygons into GeoJSON
geometries (already a transitive dependency of GeoPandas, which this project
already uses elsewhere).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import gzip
import io
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BOUNDARY_LOOKUP_BBOX_DEGREES, BBox, BoundaryProvider, best_containing_polygon, create_bbox, validate_bbox

# Adjust this import to wherever Gateway/Gateway actually live.
from urbanlens.dashboard.services.gateway import Gateway

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
    from django.contrib.gis.geos import Polygon

POLYGONS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip"
POINTS_BASE_URL = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
S2_COVERING_LEVEL = 4


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
        min_confidence: Drop rows below this model confidence score
            (dataset range is roughly 0.65-1.0). Default 0.0 keeps everything.
    """

    service_key: ClassVar[str | None] = "google_open_buildings"
    paid_service: ClassVar[bool] = False

    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    min_confidence: float = 0.0

    def get_buildings(self, bbox: BBox, *, as_geojson: bool = True) -> list[dict]:
        """Building footprint polygons overlapping ``bbox``.

        Returns GeoJSON Features by default (requires shapely). Pass
        ``as_geojson=False`` to get raw dicts with a ``geometry_wkt`` string
        instead, if you'd rather avoid the shapely dependency.
        """
        return self._download_shards(bbox, POLYGONS_BASE_URL, has_geometry=True, as_geojson=as_geojson)

    def get_building_points(self, bbox: BBox) -> list[dict]:
        """Building centroid points overlapping ``bbox`` (smaller/faster than polygons)."""
        return self._download_shards(bbox, POINTS_BASE_URL, has_geometry=False, as_geojson=False)

    def _download_shards(self, bbox: BBox, base_url: str, *, has_geometry: bool, as_geojson: bool) -> list[dict]:
        validate_bbox(bbox)
        min_lon, min_lat, max_lon, max_lat = bbox
        results: list[dict] = []
        for token in _s2_tokens_for_bbox(bbox):
            response = self.session.get(f"{base_url}/{token}_buildings.csv.gz", timeout=180)
            if response.status_code == 404:
                continue  # cell has no shard published (no buildings there)
            response.raise_for_status()
            text = gzip.decompress(response.content).decode("utf-8")
            for row in csv.DictReader(io.StringIO(text)):
                lat, lon = float(row["latitude"]), float(row["longitude"])
                if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                    continue
                confidence = float(row.get("confidence") or 0.0)
                if confidence < self.min_confidence:
                    continue
                results.append(self._row_to_output(row, has_geometry=has_geometry, as_geojson=as_geojson))
        return results

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
