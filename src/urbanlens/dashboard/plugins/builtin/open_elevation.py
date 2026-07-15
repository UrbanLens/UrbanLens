"""Open-Elevation plugin: free, open-source, keyless elevation gateway registration.

No pin-detail UI of its own - a single elevation value is a utility other
features (trip elevation profiles, terrain context) call into, not a
per-location info card on its own. Registers rate-limit defaults; see
``services.apis.elevation.open_elevation`` for the gateway.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class OpenElevationPlugin(UrbanLensPlugin):
    """Free, open-source, keyless elevation lookups."""

    name: ClassVar[str] = "open_elevation"
    verbose_name: ClassVar[str] = "Open-Elevation"
    description: ClassVar[str] = "Free, open-source, keyless elevation lookups (open-elevation.com), usable for future terrain/elevation-profile features."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Open-Elevation public instance."""
        return {
            "open_elevation": ServiceDefaults(
                display_name="Open-Elevation",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless, open-source (self-hostable via the project's Docker image).",
            ),
        }
