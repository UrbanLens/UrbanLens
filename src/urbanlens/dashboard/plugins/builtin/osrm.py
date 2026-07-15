"""OSRM plugin: free, open-source routing gateway registration.

No pin-detail UI of its own - a routing engine is a utility other features
(trip planning, "distance to nearest pin") call into, not a per-location info
card. Registers rate-limit defaults so admins can see/throttle it like any
other external call; see ``services.apis.routing.osrm`` for the gateway.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class OSRMPlugin(UrbanLensPlugin):
    """Free, open-source OSRM routing (public demo server by default)."""

    name: ClassVar[str] = "osrm"
    verbose_name: ClassVar[str] = "OSRM"
    description: ClassVar[str] = (
        "Free, open-source routing engine (project-osrm.org) over OpenStreetMap data. Uses the public "
        "demo server by default - point OSRMGateway.base_url at a self-hosted instance for production "
        "load, per the OSRM project's own guidance."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for OSRM's public demo server."""
        return {
            "osrm": ServiceDefaults(
                display_name="OSRM",
                calls_per_minute=10,
                calls_per_day=500,
                notes="Free, keyless. Public demo server is dev/testing-only per OSRM's own docs - self-host for production.",
            ),
        }
