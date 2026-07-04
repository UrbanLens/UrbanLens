"""Mapillary gateway for crowdsourced street-level imagery."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_GRAPH_URL = "https://graph.mapillary.com"
_DEFAULT_FIELDS = "id,thumb_2048_url,captured_at,compass_angle,geometry"


@dataclass(slots=True, kw_only=True)
class MapillaryGateway(StreetViewProvider):
    """Gateway for the Mapillary Graph API (street-level imagery).

    Mapillary provides crowdsourced street-level photos with global coverage,
    including many areas not reached by Google Street View.  Images are
    georeferenced and compass-headed.

    Requires: ``UL_MAPILLARY_ACCESS_TOKEN`` - a client access token from
    https://www.mapillary.com/dashboard/developers.  The free tier supports
    reasonable read-only usage.
    """

    service_key: ClassVar[str] = "mapillary"
    paid_service: ClassVar[bool] = False

    access_token: str | None = field(default_factory=lambda: settings.mapillary_access_token)

    def _auth_params(self) -> dict[str, str]:
        if not self.access_token:
            raise ValueError(
                "Mapillary access token is not set. Set UL_MAPILLARY_ACCESS_TOKEN to a client access token from https://www.mapillary.com/dashboard/developers",
            )
        return {"access_token": self.access_token}

    def search_images_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: float = 50,
        limit: int = 10,
        fields: str = _DEFAULT_FIELDS,
    ) -> dict[str, Any]:
        """Search for Mapillary images near coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres (typical max ~1000 m).
            limit: Maximum number of images to return.
            fields: Comma-separated list of response fields.

        Returns:
            Parsed JSON ``{"data": [...]}`` from the Mapillary images endpoint.
        """
        params = {
            **self._auth_params(),
            "fields": fields,
            "closeto": f"{longitude},{latitude}",
            "radius": str(radius),
            "limit": str(limit),
        }
        response = self.session.get(f"{_GRAPH_URL}/images", params=params, timeout=10)
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
        """Return Mapillary street-level images near coordinates as StreetViewSlides.

        Images are loaded directly by the browser via the ``thumb_2048_url``
        field (no server-side fetch required).

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            limit: Maximum number of slides to return.

        Yields:
            List of ``StreetViewSlide``, empty when no images are found, the
            token is not configured, or the request fails.
        """
        if not self.access_token:
            return

        try:
            data = self.search_images_near_coordinates(latitude, longitude, radius=radius, limit=limit)
        except Exception as exc:
            # TODO: Catch specific exception
            logger.warning("Mapillary search failed for %s, %s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return

        for item in data.get("data", []):
            img_url = item.get("thumb_2048_url")
            if not img_url:
                continue
            captured = item.get("captured_at") or ""
            date_str = captured[:7] if len(captured) >= 7 else (captured or "Unknown")
            geom = item.get("geometry") or {}
            coords = geom.get("coordinates") or []
            img_lon = coords[0] if len(coords) >= 2 else None
            img_lat = coords[1] if len(coords) >= 2 else None

            yield StreetViewSlide(
                img_src=img_url,
                source="Mapillary",
                date=date_str,
                heading=item.get("compass_angle"),
                latitude=img_lat,
                longitude=img_lon,
            )

    def get_image(self, image_id: str, *, fields: str = _DEFAULT_FIELDS) -> dict[str, Any]:
        """Return metadata for a single Mapillary image.

        Args:
            image_id: Mapillary image ID.
            fields: Comma-separated list of response fields.

        Returns:
            Parsed JSON image object.
        """
        response = self.session.get(
            f"{_GRAPH_URL}/{image_id}",
            params={**self._auth_params(), "fields": fields},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def search_sequences_near_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        radius: int = 100,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search for Mapillary sequences (continuous capture runs) near coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in metres.
            limit: Maximum number of sequences to return.

        Returns:
            Parsed JSON ``{"data": [...]}`` with sequence objects.
        """
        params = {
            **self._auth_params(),
            "closeto": f"{longitude},{latitude}",
            "radius": str(radius),
            "limit": str(limit),
        }
        response = self.session.get(f"{_GRAPH_URL}/image_ids", params=params, timeout=10)
        response.raise_for_status()
        return response.json()
