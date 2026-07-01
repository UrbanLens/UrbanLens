"""Esri ArcGIS REST service gateway for satellite and basemap imagery."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import cache
import requests

from urbanlens.dashboard.services.apis.locations.meta import (
    SatelliteSlide,
    SatelliteViewProvider,
    create_bbox,
)

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

_WAYBACK_CONFIG_URL = (
    "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com"
    "/waybackconfig.json"
)
_WAYBACK_EXPORT = (
    "https://wayback.maptiles.arcgis.com/arcgis/rest/services"
    "/World_Imagery/MapServer/export"
)

_WAYBACK_CACHE_KEY = "satellite_esri_wayback_releases_v3"
_WAYBACK_CACHE_TTL = 24 * 3600


@dataclass(slots=True, kw_only=True)
class EsriGateway(SatelliteViewProvider):
    """Gateway for Esri ArcGIS REST imagery services.

    Covers three sources:
    - Esri World Imagery (current, high-resolution, global)
    - USGS National Map Imagery (current, US coverage only)
    - Esri Wayback historical imagery releases (high-resolution, global)
    """

    service_key: ClassVar[str] = "esri"
    paid_service: ClassVar[bool] = False

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

        yield self.get_world_imagery_slide(bbox, width=width, height=height)
        yield self.get_usgs_slide(bbox, width=width, height=height)

        wayback_limit = 5 if limit < 0 else max(0, limit)
        yield from self.get_wayback_slides(
            bbox,
            width=width,
            height=height,
            max_count=wayback_limit,
        )

    def get_world_imagery_slide(
        self,
        bbox: str,
        *,
        width: int = 640,
        height: int = 400,
    ) -> SatelliteSlide:
        """Return a current Esri World Imagery slide."""
        return SatelliteSlide(
            img_src=(
                f"{_WORLD_IMAGERY_EXPORT}"
                f"?f=image"
                f"&imageSR=4326"
                f"&bboxSR=4326"
                f"&bbox={bbox}"
                f"&size={width},{height}"
                f"&format=jpg"
            ),
            source="Esri World Imagery",
            date="Current",
            detail="High resolution - current imagery",
        )

    def get_usgs_slide(
        self,
        bbox: str,
        *,
        width: int = 640,
        height: int = 400,
    ) -> SatelliteSlide:
        """Return a current USGS National Map imagery slide."""
        return SatelliteSlide(
            img_src=(
                f"{_USGS_EXPORT}"
                f"?f=image"
                f"&imageSR=4326"
                f"&bboxSR=4326"
                f"&bbox={bbox}"
                f"&size={width},{height}"
                f"&format=jpg"
            ),
            source="USGS National Map",
            date="Current",
            detail="High resolution - US coverage only",
        )

    def get_wayback_slides(
        self,
        bbox: str,
        *,
        width: int = 640,
        height: int = 400,
        max_count: int = 5,
    ) -> list[SatelliteSlide]:
        """Return historical Esri Wayback imagery slides."""
        if max_count <= 0:
            return []

        releases = self._get_wayback_releases()
        if not releases:
            return []

        selected = self._select_wayback_releases(releases, max_count=max_count)

        slides: list[SatelliteSlide] = []
        for release in selected:
            release_num = release.get("releaseNum")
            if release_num is None:
                continue

            date_label = release.get("releaseDateLabel") or release.get("releaseName")
            date_label = str(date_label or f"Release {release_num}")

            slides.append(
                SatelliteSlide(
                    img_src=(
                        f"{_WAYBACK_EXPORT}"
                        f"?f=image"
                        f"&imageSR=4326"
                        f"&bboxSR=4326"
                        f"&bbox={bbox}"
                        f"&size={width},{height}"
                        f"&format=jpg"
                        f"&time={release_num}"
                    ),
                    source="Esri Wayback",
                    date=date_label,
                    detail="High resolution - historical World Imagery release",
                ),
            )

        return slides

    def _get_wayback_releases(self) -> list[dict[str, Any]]:
        """Return cached Esri Wayback release metadata."""
        releases: list[dict[str, Any]] = cache.get(_WAYBACK_CACHE_KEY) or []
        if releases:
            return releases

        try:
            response = self.session.get(_WAYBACK_CONFIG_URL, timeout=10)
            if not response.ok:
                logger.debug(
                    "Could not fetch Esri Wayback config: %s %s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.debug("Could not fetch Esri Wayback config: %s", exc)
            return []

        raw_config = response.json()
        releases = self._normalize_wayback_config(raw_config)

        cache.set(_WAYBACK_CACHE_KEY, releases, _WAYBACK_CACHE_TTL)
        return releases

    def _normalize_wayback_config(
        self,
        raw_config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Normalize Esri Wayback config into a release list."""
        releases: list[dict[str, Any]] = []

        for raw_release_num, item in raw_config.items():
            if not isinstance(item, dict):
                continue

            try:
                release_num = int(raw_release_num)
            except (TypeError, ValueError):
                raw_item_release_num = item.get("releaseNum")
                if raw_item_release_num is None:
                    continue
                try:
                    release_num = int(raw_item_release_num)
                except (TypeError, ValueError):
                    continue

            releases.append(
                {
                    **item,
                    "releaseNum": release_num,
                    "releaseDateLabel": self._extract_release_date_label(item),
                },
            )

        return sorted(
            releases,
            key=lambda release: int(release.get("releaseNum") or 0),
            reverse=True,
        )

    def _extract_release_date_label(self, item: dict[str, Any]) -> str | None:
        """Extract a human-readable Wayback release date."""
        release_date_label = item.get("releaseDateLabel")
        if release_date_label:
            return str(release_date_label)

        item_title = str(item.get("itemTitle") or "")
        prefix = "World Imagery (Wayback "
        suffix = ")"

        if item_title.startswith(prefix) and item_title.endswith(suffix):
            return item_title.removeprefix(prefix).removesuffix(suffix)

        return None

    def _select_wayback_releases(
        self,
        releases: list[dict[str, Any]],
        *,
        max_count: int,
    ) -> list[dict[str, Any]]:
        """Select evenly-spaced Wayback releases, newest first."""
        if len(releases) <= max_count:
            return releases

        step = max(1, len(releases) // max_count)
        return releases[::step][:max_count]
