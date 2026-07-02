"""OpenAerialMap gateway for openly licensed aerial imagery."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, create_bbox_str

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openaerialmap.org"


@dataclass(slots=True, kw_only=True)
class OpenAerialMapGateway(SatelliteViewProvider):
    """Gateway for OpenAerialMap imagery metadata and tile indexes.

    OpenAerialMap (OAM) is a catalog of openly licensed aerial and satellite
    imagery contributed by NGOs, governments, and individuals.  No API key is
    required.
    """

    service_key: ClassVar[str] = "open_aerial_map"
    paid_service: ClassVar[bool] = False

    def search_imagery_for_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
        limit: int = 10,
        sort: str = "-acquisition_end",
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Search OpenAerialMap image metadata around coordinates.

        Args:
            latitude: WGS-84 latitude of the centre point.
            longitude: WGS-84 longitude of the centre point.
            delta: Half-width of the search bounding box in degrees.
            limit: Maximum number of results to return.
            sort: Sort order (default ``"-acquisition_end"`` = newest first).
            provider: Optional provider name filter.

        Returns:
            Parsed JSON with a ``results`` list of imagery metadata objects.
            Each result includes ``thumbnail``, ``tms`` tile URL, ``title``,
            ``provider``, ``acquisition_start``, and ``acquisition_end`` fields.
        """
        limit = limit if limit > 0 else 10
        params: dict[str, Any] = {"bbox": create_bbox_str(latitude, longitude, delta), "limit": limit, "sort": sort}
        if provider:
            params["provider"] = provider
        response = self.session.get(f"{_BASE_URL}/meta", params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 17,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        """Return OpenAerialMap imagery as SatelliteSlides.

        Uses the ``thumbnail`` URL from each result as the image source, so
        these slides are loaded directly by the browser (no server-side fetch).

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            limit: Maximum number of slides to return.

        Returns:
            List of SatelliteSlide, empty when no imagery is available or the
            request fails.
        """
        try:
            data = self.search_imagery_for_coordinates(latitude, longitude, delta=0.005, limit=limit)
        except Exception as exc:
            # TODO: Catch specific exception
            logger.warning("OpenAerialMap search failed for %s, %s: %s", latitude, longitude, exc)
            return

        for item in (data.get("results") or []):
            thumbnail = item.get("thumbnail")
            if not thumbnail:
                continue
            provider = item.get("provider") or "OpenAerialMap"
            acq_end = (item.get("acquisition_end") or "")[:10]
            date_str = acq_end or "Unknown"
            resolution = item.get("gsd")
            detail = f"{resolution:.2f} m/px" if resolution else "Open licensed aerial imagery"
            yield SatelliteSlide(
                img_src=thumbnail,
                source=f"OAM / {provider}",
                date=date_str,
                detail=detail,
            )

    def list_tile_services_for_coordinates(
        self,
        latitude: float,
        longitude: float,
        *,
        delta: float = 0.005,
        limit: int = 10,
    ) -> dict[str, Any]:
        """List available OpenAerialMap TMS services around coordinates.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            delta: Half-width of the search bounding box in degrees.
            limit: Maximum number of TMS entries to return.

        Returns:
            Parsed JSON with TMS service descriptors.
        """
        response = self.session.get(
            f"{_BASE_URL}/tms",
            params={"bbox": create_bbox_str(latitude, longitude, delta), "limit": str(limit)},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def imagery_statistics_for_bbox(self, west: float, south: float, east: float, north: float) -> dict[str, Any]:
        """Return OpenAerialMap analytics for a bounding box.

        Args:
            west: Western longitude of the bounding box.
            south: Southern latitude of the bounding box.
            east: Eastern longitude of the bounding box.
            north: Northern latitude of the bounding box.

        Returns:
            Parsed JSON with aggregate statistics for the bbox.
        """
        response = self.session.get(
            f"{_BASE_URL}/analytics",
            params={"bbox": f"{west},{south},{east},{north}"},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
