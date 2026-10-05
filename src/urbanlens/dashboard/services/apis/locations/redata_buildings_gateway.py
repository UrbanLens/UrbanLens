"""Gateway for REData's ``/buildings/`` near-a-coordinate endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_BUILDINGS_PATH = "/api/v1/buildings/"


@dataclass(slots=True, kw_only=True)
class RedataBuildingsGateway(RedataLocationContextGateway):
    """REST client for REData's standalone building-footprint lookup."""

    service_key: ClassVar[str] = "redata_buildings"

    def find_near(self, latitude: float, longitude: float, *, provider: str, radius_meters: float | None = None) -> list[dict[str, Any]]:
        """Return building footprints from one REData provider near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            provider: The provider tag, e.g. ``"overture"`` for REData's Overture Maps mirror. Named rather than
                left to REData's default, which is every provider it registers for this endpoint.
            radius_meters: Search radius; omit for REData's default.

        Returns:
            ``BuildingRecord``-shaped dicts (``source``, ``name``, ``geometry`` as GeoJSON, ``attributes``,
            ``distance_meters``, ...), nearest first; possibly empty. For ``overture``, ``attributes`` is the
            Overture row's own properties (``subtype``, ``class``, ``height``, ``num_floors``, ``names``, ...).

        Raises:
            LocationContextUnavailableError: The provider did not answer, or the request itself failed.
        """
        return self.near_point(_BUILDINGS_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider).results
