"""OpenWeatherMap plugin: the keyed weather provider, asked ahead of the keyless Open-Meteo fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class OpenWeatherMapPlugin(UrbanLensPlugin):
    """OpenWeatherMap forecasts, when ``UL_OPENWEATHERMAP_API_KEY`` is set."""

    name: ClassVar[str] = "openweathermap"
    verbose_name: ClassVar[str] = "OpenWeatherMap"
    description: ClassVar[str] = "Weather forecasts for the Private Pin page and trips from OpenWeatherMap, used ahead of Open-Meteo when a key is configured."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """OpenWeatherMap as a weather provider, between a first-party source and Open-Meteo."""
        from urbanlens.dashboard.plugins.points import DATA_SOURCE_CREDITS, DataSourceCredit, MapAttribution

        plugin.add(
            DATA_SOURCE_CREDITS,
            DataSourceCredit(
                name="OpenWeatherMap",
                category="planning",
                url="https://openweathermap.org/",
                note="Precipitation and cloud overlay tiles on the optional weather layer.",
                attribution=MapAttribution(icon="cloud", text="Map data © OpenWeatherMap, subject to their", terms_label="terms of use", terms_url="https://openweathermap.org/terms"),
            ),
        )

        from urbanlens.dashboard.plugins.points import WEATHER_PROVIDERS
        from urbanlens.dashboard.services.apis.locations.weather_resolution import OpenWeatherMapProvider

        plugin.add(WEATHER_PROVIDERS, OpenWeatherMapProvider(), priority=50)
