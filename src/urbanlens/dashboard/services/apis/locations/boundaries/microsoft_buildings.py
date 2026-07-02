"""Gateway for Microsoft's Global ML Building Footprints dataset.

Unlike Regrid, this isn't a query API -- Microsoft publishes ~1.4B building
footprint polygons as gzip-compressed, line-delimited GeoJSON files, sharded
by country and Bing-Maps quadkey, and lists the download URL for every shard
in a public CSV (``dataset-links.csv``) on GitHub:
https://github.com/microsoft/GlobalMLBuildingFootprints

This gateway resolves which quadkey shard(s) cover a bounding box, downloads
just those shards over plain HTTPS, and filters to features that actually
overlap the box. It still extends ``Gateway`` (and still benefits
from ``self.session``'s rate limiting/logging) even though there's no auth
token -- every request here is an ordinary GET against GitHub/Azure Blob
Storage.

Note: Microsoft partitions the current dataset-links.csv at Bing tile zoom
level 9. Microsoft has changed this scheme before, so if lookups start
turning up empty, double check the zoom level implied by the QuadKey values
in the current CSV against ``quadkey_zoom`` below.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import gzip
import io
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BOUNDARY_LOOKUP_BBOX_DEGREES, BBox, BoundaryProvider, _best_containing_polygon, create_bbox, feature_intersects_bbox, validate_bbox
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Web Mercator lon/lat -> Bing/Slippy-map tile (x, y) at a given zoom."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def _tile_to_quadkey(x: int, y: int, zoom: int) -> str:
    """Bing tile system: (x, y, zoom) -> quadkey string."""
    digits = []
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        digit = (1 if x & mask else 0) + (2 if y & mask else 0)
        digits.append(str(digit))
    return "".join(digits)


def quadkeys_for_bbox(bbox: BBox, *, zoom: int) -> set[str]:
    """All quadkeys at ``zoom`` whose tiles intersect ``bbox``."""
    min_lon, min_lat, max_lon, max_lat = bbox
    x_min, y_max = _lonlat_to_tile(min_lon, min_lat, zoom)
    x_max, y_min = _lonlat_to_tile(max_lon, max_lat, zoom)
    return {
        _tile_to_quadkey(x, y, zoom)
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1)
        for y in range(min(y_min, y_max), max(y_min, y_max) + 1)
    }


@dataclass(slots=True, kw_only=True)
class MicrosoftBuildingFootprintsGateway(Gateway, BoundaryProvider):
    """Fetch building footprint polygons from Microsoft's open dataset.

    Attributes:
        quadkey_zoom: Bing tile zoom level the current dataset-links.csv is
            partitioned at. Defaults to 9; see module docstring.
    """

    service_key: ClassVar[str | None] = "microsoft_building_footprints"
    paid_service: ClassVar[bool] = False

    quadkey_zoom: int = 9
    _dataset_links: list[dict[str, str]] | None = field(default=None, repr=False)
    bbox_delta: float = BOUNDARY_LOOKUP_BBOX_DEGREES
    
    def _load_dataset_links(self) -> list[dict[str, str]]:
        if self._dataset_links is None:
            # Load from {PROJECT_ROOT}/data/dataset-links.csv
            dataset_file = settings.project_root / "data" / "dataset-links.csv"
            self._dataset_links = list(csv.DictReader(io.StringIO(dataset_file.read_text())))
        return self._dataset_links

    def list_available_locations(self) -> set[str]:
        """Every country/region name (the ``Location`` column) with coverage."""
        return {row["Location"] for row in self._load_dataset_links() if row.get("Location")}

    def get_buildings(self, bbox: BBox, *, country: str | None = None) -> list[dict]:
        """Download and return building footprint Features overlapping ``bbox``.

        Args:
            bbox: Area of interest.
            country: Optional exact match against the dataset's ``Location``
                column (see ``list_available_locations``) to disambiguate
                shards near country borders and skip irrelevant downloads.
        """
        validate_bbox(bbox)
        candidate_quadkeys = quadkeys_for_bbox(bbox, zoom=self.quadkey_zoom)
        rows = self._load_dataset_links()
        matches = [
            row for row in rows
            if row.get("QuadKey") in candidate_quadkeys
            and (country is None or row.get("Location") == country)
        ]

        features: list[dict] = []
        for row in matches:
            response = self.session.get(row["Url"], timeout=180)
            response.raise_for_status()
            raw = gzip.decompress(response.content)
            for line in raw.splitlines():
                stripped_line = line.strip()
                if not stripped_line:
                    continue
                parsed = json.loads(stripped_line)
                feature = (
                    parsed if parsed.get("type") == "Feature"
                    else {"type": "Feature", "geometry": parsed, "properties": {}}
                )
                if feature_intersects_bbox(feature, bbox):
                    features.append(feature)
        return features

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        return _best_containing_polygon(
            self.get_buildings(create_bbox(latitude, longitude, self.bbox_delta)),
            latitude,
            longitude,
        )
