"""
Google Photos plugin: rate-limit defaults for the per-user picker integration.
This plugin registers the service's rate-limit defaults so calls are throttled and logged like every other external API."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class GooglePhotosPlugin(UrbanLensPlugin):
    """Google Photos integration: per-user Picker-API photo import."""

    name: ClassVar[str] = "google_photos"
    verbose_name: ClassVar[str] = "Google Photos"
    description: ClassVar[str] = "Lets a user pick photos from their Google Photos library (via Google's own Picker UI) to import onto a pin."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 42

    def setup(self, plugin: PluginContext) -> None:
        """Rate-limit defaults for the Google Photos Picker API."""
        from urbanlens.dashboard.plugins.points import SERVICE_DEFAULTS

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "google_photos": ServiceDefaults(
                    display_name="Google Photos Picker API",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=30,
                    calls_per_day=2000,
                    notes="Free API; Google quota is per-user. Session polling is the bulk of the call volume.",
                ),
            },
        )
