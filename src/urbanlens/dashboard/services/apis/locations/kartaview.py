"""KartaView (formerly OpenStreetCam) gateway for open street-level imagery."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.meta import StreetViewSlide
from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.kartaview.org/2.0"
_STORAGE_URL = "https://storage.kartaview.com"


@dataclass(frozen=True, slots=True, kw_only=True)
class KartaViewGateway(Gateway):
    """Gateway for the KartaView street-level imagery API.

    KartaView (formerly OpenStreetCam) provides crowdsourced street-level
    photos with global coverage.  No API key is required for read-only access.
    """

    service_key: ClassVar[str] = "kartaview"

    def search_photos_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: float = 50,
        page: int = 1,
        items_per_page: int = 10,
    ) -> dict[str, Any]:
        """Return KartaView photos near coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            page: Page number for pagination.
            items_per_page: Number of results per page.

        Returns:
            Parsed JSON response.  Nearby photos are in ``currentPageItems``.
        """
        response = self.session.post(
            f"{_BASE_URL}/photo/nearby-photos/",
            json={
                "lat": latitude,
                "lng": longitude,
                "radius": radius,
                "page": page,
                "itemsPerPage": items_per_page,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_street_view_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: float = 50,
        limit: int = 5,
    ) -> list[StreetViewSlide]:
        """Return KartaView photos near coordinates as StreetViewSlides.

        Images are loaded directly by the browser — no server-side fetch.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            limit: Maximum number of slides to return.

        Returns:
            List of ``StreetViewSlide``, empty when no images are found or the
            request fails.
        """
        try:
            data = self.search_photos_near_coordinates(latitude, longitude, radius=radius, items_per_page=limit)
        except Exception as exc:
            logger.warning("KartaView search failed for %s, %s: %s", latitude, longitude, exc)
            return []

        slides = []
        for photo in (data.get("currentPageItems") or [])[:limit]:
            file_path = (
                photo.get("fileurlLTh")
                or photo.get("fileurlProc")
                or photo.get("filepathProc")
                or ""
            )
            if not file_path:
                continue
            if not file_path.startswith("http"):
                file_path = f"{_STORAGE_URL}/{file_path.lstrip('/')}"
            date_str = (photo.get("dateAdded") or "")[:10] or "Unknown"
            try:
                heading = float(photo["heading"]) if photo.get("heading") is not None else None
                img_lat = float(photo["lat"]) if photo.get("lat") is not None else None
                img_lon = float(photo["lng"]) if photo.get("lng") is not None else None
            except (ValueError, TypeError):
                heading = img_lat = img_lon = None
            slides.append(StreetViewSlide(
                img_src=file_path,
                source="KartaView",
                date=date_str,
                heading=heading,
                latitude=img_lat,
                longitude=img_lon,
            ))
        return slides

    def get_sequence(self, sequence_id: str) -> dict[str, Any]:
        """Return metadata for a KartaView sequence.

        Args:
            sequence_id: KartaView sequence identifier.

        Returns:
            Parsed JSON sequence object with photo list and track geometry.
        """
        response = self.session.get(f"{_BASE_URL}/sequence/{sequence_id}/", timeout=10)
        response.raise_for_status()
        return response.json()

    def get_photo(self, photo_id: str) -> dict[str, Any]:
        """Return metadata for a single KartaView photo.

        Args:
            photo_id: KartaView photo identifier.

        Returns:
            Parsed JSON photo object.
        """
        response = self.session.get(f"{_BASE_URL}/photo/{photo_id}/", timeout=10)
        response.raise_for_status()
        return response.json()
