"""Open-Meteo plugin: free, keyless weather forecast fallback.

Registers Open-Meteo's rate-limit defaults for the admin API-limits page.
The actual wiring is a plain fallback inside ``PinController.weather_forecast``
(see ``services.apis.weather.forecast``/``open_meteo``) rather than a typed
plugin contribution point - there is no ``get_weather_providers`` hook today,
matching how the existing OpenWeatherMap gateway also has no plugin of its own.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class OpenMeteoPlugin(UrbanLensPlugin):
    """Free, keyless weather forecast redundancy for OpenWeatherMap."""

    name: ClassVar[str] = "open_meteo"
    verbose_name: ClassVar[str] = "Open-Meteo"
    description: ClassVar[str] = "Free, keyless weather forecast API - used automatically as a fallback for the pin detail page's weather widget when OpenWeatherMap isn't configured or fails."
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
