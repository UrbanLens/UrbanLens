"""OSRM gateway - free, open-source routing.

http://project-osrm.org/ - self-hostable routing engine over OpenStreetMap
data. ``base_url`` comes from ``UL_OSRM_BASE_URL`` and falls back to the public
demo server (router.project-osrm.org), which the OSRM project itself documents
as dev/testing use only - rate-limited, with no uptime guarantee. A deployment
whose drive-time answers matter should set that variable to a self-hosted
instance (``docker run osrm/osrm-backend`` with a pre-processed ``.osrm``
extract). No API key is required either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar, Literal

import requests

from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: OSRM's public demo instance - fine for development, not for production load.
_DEMO_BASE_URL = "https://router.project-osrm.org"

OsrmProfile = Literal["driving", "walking", "cycling"]


@dataclass(slots=True, kw_only=True)
class OSRMGateway(Gateway):
    """Gateway for an OSRM routing server (public demo or self-hosted)."""

    service_key: ClassVar[str] = "osrm"
    paid_service: ClassVar[bool] = False

    # default_factory rather than a bare default, for the same reason the weather
    # gateway's api_key uses one: a bare default is evaluated once at import, so a
    # settings change or a test patch would never reach later instantiations.
    base_url: str = field(default_factory=lambda: settings.osrm_base_url or _DEMO_BASE_URL)

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
        url = f"{self.base_url.rstrip('/')}/route/v1/{profile}/{coordinates}"
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
