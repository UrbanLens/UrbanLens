"""Open-Meteo plugin: free, keyless weather forecast fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class OpenMeteoPlugin(UrbanLensPlugin):
    """Free, keyless weather forecast redundancy for OpenWeatherMap."""

    name: ClassVar[str] = "open_meteo"
    verbose_name: ClassVar[str] = "Open-Meteo"
    description: ClassVar[str] = "Free, keyless weather forecast API - used automatically as a fallback for the Private Pin page's weather widget when OpenWeatherMap isn't configured or fails."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """Rate-limit defaults for the Open-Meteo API, and Open-Meteo as the weather provider of last resort."""
        from urbanlens.dashboard.plugins.points import SERVICE_DEFAULTS, WEATHER_PROVIDERS
        from urbanlens.dashboard.services.apis.locations.weather_resolution import OpenMeteoProvider

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "open_meteo": ServiceDefaults(
                    display_name="Open-Meteo",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=20,
                    calls_per_day=1000,
                    notes="Free, keyless weather API - no account required.",
                ),
            },
        )
        plugin.add(WEATHER_PROVIDERS, OpenMeteoProvider(), priority=100)
