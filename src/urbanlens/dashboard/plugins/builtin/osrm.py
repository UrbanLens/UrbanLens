"""
OSRM plugin: free, open-source routing gateway registration.
Registers rate-limit defaults so admins can see/throttle it like any other external call; see ``services.apis.routing.osrm`` for the gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class OSRMPlugin(UrbanLensPlugin):
    """Free, open-source OSRM routing (public demo server by default)."""

    name: ClassVar[str] = "osrm"
    verbose_name: ClassVar[str] = "OSRM"
    description: ClassVar[str] = (
        "Free, open-source routing engine (project-osrm.org) over OpenStreetMap data. Uses the public demo server unless UL_OSRM_BASE_URL points at a self-hosted instance, which is what the OSRM project's own guidance asks for under production load."
    )
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """Rate-limit defaults for OSRM's public demo server, and OSRM as a route provider."""
        from urbanlens.dashboard.plugins.points import ROUTE_PROVIDERS, SERVICE_DEFAULTS
        from urbanlens.dashboard.services.apis.locations.routing_resolution import OsrmRouteProvider

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "osrm": ServiceDefaults(
                    display_name="OSRM",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=10,
                    calls_per_day=500,
                    notes="Free, keyless. Public demo server is dev/testing-only per OSRM's own docs - self-host for production.",
                ),
            },
        )
        plugin.add(ROUTE_PROVIDERS, OsrmRouteProvider(), priority=100)
