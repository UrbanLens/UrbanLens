"""Open-Meteo plugin: free, keyless weather forecast fallback."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults


class OpenMeteoPlugin(UrbanLensPlugin):
    """Free, keyless weather forecast redundancy for OpenWeatherMap."""

    name: ClassVar[str] = "open_meteo"
    verbose_name: ClassVar[str] = "Open-Meteo"
    description: ClassVar[str] = "Free, keyless weather forecast API - used automatically as a fallback for the Private Pin page's weather widget when OpenWeatherMap isn't configured or fails."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Open-Meteo API."""
        return {
            "open_meteo": ServiceDefaults(
                display_name="Open-Meteo",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless weather API - no account required.",
            ),
        }
