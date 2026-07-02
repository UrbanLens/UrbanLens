"""KartaView (formerly OpenStreetCam) gateway for open street-level imagery."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.kartaview.org/2.0"
_STORAGE_URL = "https://storage.kartaview.com"


@dataclass(slots=True, kw_only=True)
class KartaViewGateway(StreetViewProvider):
    """Gateway for the KartaView street-level imagery API.

    KartaView (formerly OpenStreetCam) provides crowdsourced street-level
    photos with global coverage.  No API key is required for read-only access.
    """

    service_key: ClassVar[str] = "kartaview"
    paid_service: ClassVar[bool] = False

    def search_sequences_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: float = 50,
        page: int = 1,
        items_per_page: int = 10,
    ) -> dict[str, Any]:
        """Return KartaView sequences near coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            page: Page number for pagination.
            items_per_page: Number of results per page.

        Returns:
            Parsed JSON response.  Nearby sequences are in ``currentPageItems``.
        """
        response = self.session.post(
            f"{_BASE_URL}/sequence/nearby-sequences/",
            data={
                "lat": latitude,
                "lng": longitude,
                "radius": radius,
                "page": page,
                "ipp": items_per_page,
            },
            timeout=10,
        )
        if not response.ok:
            logger.warning(
                "KartaView error %s for %s: %s",
                response.status_code,
                response.url,
                response.text,
            )
        response.raise_for_status()
        return response.json()

    def _generate_street_view_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: float = 50,
        limit: int = 5,
    ) -> Generator[StreetViewSlide]:
        """Return KartaView photos near coordinates as StreetViewSlides.

        Images are loaded directly by the browser — no server-side fetch.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            limit: Maximum number of slides to return.

        Yields:
            List of ``StreetViewSlide``, empty when no images are found or the
            request fails.
        """
        try:
            data = self.search_sequences_near_coordinates(latitude, longitude, radius=radius, items_per_page=limit)
        except Exception as exc:
            # TODO: Catch specific exception
            logger.warning("KartaView search failed for %s, %s: %s", latitude, longitude, exc)
            return

        current_page_items = data.get("currentPageItems") or []
        if limit > 0 and len(current_page_items) > limit:
            current_page_items = current_page_items[:limit]

        for sequence in current_page_items:
            # v2 API returns sequences; use the sequence thumbnail as the slide image
            file_path = sequence.get("thumbHead") or ""
            if not file_path:
                continue
            if not file_path.startswith("http"):
                file_path = f"{_STORAGE_URL}/{file_path.lstrip('/')}"
            date_str = (sequence.get("dateAdded") or "")[:10] or "Unknown"
            try:
                img_lat = float(sequence["lat"]) if sequence.get("lat") is not None else None
                img_lon = float(sequence["lng"]) if sequence.get("lng") is not None else None
            except (ValueError, TypeError):
                img_lat = img_lon = None

            yield StreetViewSlide(
                img_src=file_path,
                source="KartaView",
                date=date_str,
                heading=None,
                latitude=img_lat,
                longitude=img_lon,
            )

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
