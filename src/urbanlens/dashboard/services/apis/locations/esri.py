"""Esri ArcGIS REST service gateway for satellite and basemap imagery."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, ClassVar

from django.core.cache import cache
import requests

from urbanlens.dashboard.services.apis.locations.meta import SatelliteSlide, SatelliteViewProvider, create_bbox
from urbanlens.dashboard.services.gateway import Gateway

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_WORLD_IMAGERY_EXPORT = (
    "https://server.arcgisonline.com/arcgis/rest/services"
    "/World_Imagery/MapServer/export"
)
_USGS_EXPORT = (
    "https://basemap.nationalmap.gov/arcgis/rest/services"
    "/USGSImageryOnly/MapServer/export"
)
_WAYBACK_RELEASES_URL = (
    "https://wayback.maptiles.esri.com/arcgis/rest/services"
    "/World_Imagery_Wayback/MapServer/releases"
)
_WAYBACK_EXPORT = (
    "https://wayback.maptiles.esri.com/arcgis/rest/services"
    "/World_Imagery_Wayback/MapServer/export"
)
_WAYBACK_CACHE_KEY = "satellite_esri_wayback_releases_v2"
_WAYBACK_CACHE_TTL = 24 * 3600


@dataclass(frozen=True, slots=True, kw_only=True)
class EsriGateway(SatelliteViewProvider):
    """Gateway for Esri ArcGIS REST imagery services.

    Covers three sources:
    - Esri World Imagery (current, high-resolution, global)
    - USGS National Map Imagery (current, US coverage only)
    - Esri Wayback historical imagery releases (high-resolution, global)
    """

    service_key: ClassVar[str] = "esri"
        
    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 18,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        """Return a list of current Esri World Imagery slides for the given bounding box.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            zoom: Zoom level (1-22).
            width: Image width in pixels (max 1280).
            height: Image height in pixels (max 1280).

        Returns:
            List of SatelliteSlide, empty when no imagery is available or the request fails.
        """
        bbox = create_bbox(latitude, longitude)
        
        yield self.get_world_imagery_slide(bbox)
        yield self.get_usgs_slide(bbox)
        yield from self.get_wayback_slides(bbox)
        
    def get_world_imagery_slide(self, bbox: str) -> SatelliteSlide:
        """Return a current Esri World Imagery slide for the given bounding box.

        Args:
            bbox: Bounding box in ``lng_min,lat_min,lng_max,lat_max`` format (EPSG:4326).

        Returns:
            SatelliteSlide whose image is fetched directly by the browser.
        """
        return SatelliteSlide(
            img_src=(
                f"{_WORLD_IMAGERY_EXPORT}"
                f"?f=image&imageSR=4326&bboxSR=4326&bbox={bbox}&size=640,400&format=jpg"
            ),
            source="Esri World Imagery",
            date="Current",
            detail="High resolution - current imagery",
        )
        
    def get_usgs_slide(self, bbox: str) -> SatelliteSlide:
        """Return a current USGS National Map imagery slide for the given bounding box.

        Args:
            bbox: Bounding box in ``lng_min,lat_min,lng_max,lat_max`` format (EPSG:4326).

        Returns:
            SatelliteSlide whose image is fetched directly by the browser.
            Only returns imagery for locations within the United States.
        """
        return SatelliteSlide(
            img_src=(
                f"{_USGS_EXPORT}"
                f"?bbox={bbox}&bboxSR=4326&imageSR=4326&size=640,400&f=image"
            ),
            source="USGS National Map",
            date="Current",
            detail="High resolution - US coverage only",
        )

    def get_wayback_slides(self, bbox: str, max_count: int = 5) -> list[SatelliteSlide]:
        """Return historical Esri Wayback imagery slides for the given bounding box.

        Fetches the full release list once and caches it for 24 hours, then selects
        up to ``max_count`` evenly-spaced releases from newest to oldest.

        Args:
            bbox: Bounding box in ``lng_min,lat_min,lng_max,lat_max`` format (EPSG:4326).
            max_count: Maximum number of historical slides to return.

        Returns:
            List of SatelliteSlide, one per selected release, newest first.
            Returns an empty list when the release list cannot be fetched.
        """
        releases: list[dict] = cache.get(_WAYBACK_CACHE_KEY) or []
        if not releases:
            try:
                resp = self.session.get(_WAYBACK_RELEASES_URL, params={"f": "json"}, timeout=10)
                resp.raise_for_status()
                releases = resp.json().get("releases", [])
                cache.set(_WAYBACK_CACHE_KEY, releases, _WAYBACK_CACHE_TTL)
            except requests.exceptions.RequestException as exc:
                logger.warning("Could not fetch Esri Wayback releases: %s", exc)
                return []

        sorted_releases = sorted(releases, key=lambda r: r.get("releaseName", ""), reverse=True)
        step = max(1, len(sorted_releases) // max_count)
        selected = sorted_releases[::step][:max_count]

        slides = []
        for rel in selected:
            rid = rel.get("id")
            if not rid:
                continue
            slides.append(SatelliteSlide(
                img_src=(
                    f"{_WAYBACK_EXPORT}"
                    f"?f=image&imageSR=4326&bboxSR=4326&bbox={bbox}&size=640,400&layers=show:{rid}"
                ),
                source="Esri Wayback",
                date=rel.get("releaseName", f"Release {rid}"),
                detail="High resolution - historical snapshot",
            ))
        return slides
