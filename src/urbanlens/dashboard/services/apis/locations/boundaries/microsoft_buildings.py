"""Gateway for Microsoft's Global ML Building Footprints dataset.
Microsoft has changed this scheme before, so if lookups start turning up empty, double check the zoom level implied by the QuadKey values in the current CSV against ``quadkey_zoom`` below."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import gzip
import io
import json
import logging
import math
import re
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BOUNDARY_LOOKUP_BBOX_DEGREES, BBox, BoundaryProvider, best_containing_polygon, create_bbox, feature_intersects_bbox, validate_bbox
from urbanlens.dashboard.services.apis.locations.boundaries.shards import read_shard
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.contrib.gis.geos import Polygon

#: Parts run to 292 MB compressed where the dataset is dense (Tokyo's; one in the US is 170 MB); one past this is skipped.
MAX_PART_BYTES = 64 * 1024 * 1024
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
#: Far wider than any building, so one whose first vertex lies further than this from the box cannot overlap it.
_NEAR_DEGREES = 0.05
_FIRST_VERTEX = re.compile(rb"\[\s*(-?[0-9][0-9.eE+-]*)\s*,\s*(-?[0-9][0-9.eE+-]*)")

logger = logging.getLogger(__name__)


def _listed_bytes(size: str) -> float | None:
    """The links file's ``Size`` (``74.7KB``, ``16.0MB``) in bytes, or None when it is not one."""
    match = re.fullmatch(r"([0-9.]+)\s*(B|KB|MB|GB)", size.strip())
    return float(match[1]) * _SIZE_UNITS[match[2]] if match else None


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
    return {_tile_to_quadkey(x, y, zoom) for x in range(min(x_min, x_max), max(x_min, x_max) + 1) for y in range(min(y_min, y_max), max(y_min, y_max) + 1)}


@dataclass(slots=True, kw_only=True)
class MicrosoftBuildingFootprintsGateway(Gateway, BoundaryProvider):
    """Fetch building footprint polygons from Microsoft's open dataset.

    Attributes:
        quadkey_zoom: Bing tile zoom level the current dataset-links.csv is partitioned at."""

    service_key: ClassVar[str | None] = "microsoft_building_footprints"
    paid_service: ClassVar[bool] = False
    boundary_kind: ClassVar[str] = "building"

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
        matches = [row for row in rows if row.get("QuadKey") in candidate_quadkeys and (country is None or row.get("Location") == country)]

        features: list[dict] = []
        for row in matches:
            listed = _listed_bytes(row.get("Size", ""))
            if listed is not None and listed > MAX_PART_BYTES:
                logger.info("Skipping the Microsoft footprints part %s: the links file lists it at %s", row["Url"], row["Size"])
                continue
            part = read_shard(self.session, row["Url"], max_bytes=MAX_PART_BYTES, what="A Microsoft footprints part")
            if part is None:
                continue
            with gzip.GzipFile(fileobj=io.BytesIO(part)) as lines:
                features.extend(_features_within(lines, bbox))
        return features

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        return best_containing_polygon(
            self.get_buildings(create_bbox(latitude, longitude, self.bbox_delta)),
            latitude,
            longitude,
        )


def _features_within(lines: Iterable[bytes], bbox: BBox) -> Iterator[dict]:
    """The features among a part's GeoJSON lines that overlap *bbox*, read one line at a time."""
    for line in lines:
        stripped = line.strip()
        if not stripped or _far_from(stripped, bbox):
            continue
        parsed = json.loads(stripped)
        feature = parsed if parsed.get("type") == "Feature" else {"type": "Feature", "geometry": parsed, "properties": {}}
        if feature_intersects_bbox(feature, bbox):
            yield feature


def _far_from(line: bytes, bbox: BBox) -> bool:
    """Whether a GeoJSON line's first vertex lies too far from *bbox* for its building to overlap it.

    Only a single polygon is judged by its first vertex: a later part of a multi-part geometry can lie anywhere.
    """
    if b"Multi" in line or b"Collection" in line:
        return False
    match = _FIRST_VERTEX.search(line)
    if match is None:
        return False
    try:
        lon, lat = float(match[1]), float(match[2])
    except ValueError:
        return False
    min_lon, min_lat, max_lon, max_lat = bbox
    return not (min_lon - _NEAR_DEGREES <= lon <= max_lon + _NEAR_DEGREES and min_lat - _NEAR_DEGREES <= lat <= max_lat + _NEAR_DEGREES)
