"""OSRM gateway - free, open-source routing.

http://project-osrm.org/ - self-hostable routing engine over OpenStreetMap
data. ``base_url`` defaults to the public demo server
(router.project-osrm.org), which the OSRM project itself documents as
dev/testing use only; production installs should point ``base_url`` at a
self-hosted instance (``docker run osrm/osrm-backend`` with a pre-processed
``.osrm`` extract). No API key is required either way.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar, Literal

import requests

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

#: OSRM's public demo instance - fine for development, not for production load.
_DEMO_BASE_URL = "https://router.project-osrm.org"

OsrmProfile = Literal["driving", "walking", "cycling"]


@dataclass(slots=True, kw_only=True)
class OSRMGateway(Gateway):
    """Gateway for an OSRM routing server (public demo or self-hosted)."""

    service_key: ClassVar[str] = "osrm"
    paid_service: ClassVar[bool] = False

    base_url: str = _DEMO_BASE_URL

    def get_route(self, waypoints: list[tuple[float, float]], *, profile: OsrmProfile = "driving") -> dict[str, Any] | None:
        """Return the routed distance/duration between an ordered list of waypoints.

        Args:
            waypoints: Ordered ``(latitude, longitude)`` pairs, at least two.
            profile: Routing profile - ``"driving"``, ``"walking"``, or ``"cycling"``.

        Returns:
            Dict with ``distance_meters``, ``duration_seconds``, and
            ``geometry`` (``None`` here since overview geometry isn't
            requested), or None when routing failed (e.g. no road network
            connects the points, or the request failed).
        """
        if len(waypoints) < 2:
            raise ValueError("get_route requires at least two waypoints")

        coordinates = ";".join(f"{longitude},{latitude}" for latitude, longitude in waypoints)
        url = f"{self.base_url}/route/v1/{profile}/{coordinates}"
        try:
            response = self.session.get(url, params={"overview": "false", "alternatives": "false", "steps": "false"}, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("OSRM routing failed for %d waypoints", len(waypoints), exc_info=True)
            return None

        if body.get("code") != "Ok" or not body.get("routes"):
            logger.debug("OSRM returned no route: %s", body.get("code"))
            return None

        route = body["routes"][0]
        return {"distance_meters": route.get("distance"), "duration_seconds": route.get("duration")}

    def get_route_between(self, origin: tuple[float, float], destination: tuple[float, float], *, profile: OsrmProfile = "driving") -> dict[str, Any] | None:
        """Convenience wrapper around :meth:`get_route` for a single origin/destination pair.

        Args:
            origin: ``(latitude, longitude)`` of the starting point.
            destination: ``(latitude, longitude)`` of the destination.
            profile: Routing profile - ``"driving"``, ``"walking"``, or ``"cycling"``.

        Returns:
            Same shape as :meth:`get_route`.
        """
        return self.get_route([origin, destination], profile=profile)
