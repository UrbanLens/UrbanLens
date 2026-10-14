"""
Resolves weather/sun-times lookups to the first provider that can answer, per call.
Every provider converts into the same ``ForecastSlot``/``SunTimes`` shapes (``services.apis.weather.forecast``), so callers never branch on which provider actually answered."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot, SunTimes, owm_item_to_slot
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from datetime import date

logger = logging.getLogger(__name__)


class WeatherProvider(ABC):
    """
    One source of forecasts and sun times, and optionally of recorded weather.

    A provider that cannot answer declines (None) rather than raising, so the chain moves on; it logs why.

    Attributes:
        key: Short identifier, for logs.
        supports_history: Whether :meth:`historical_days` answers.
    """

    key: ClassVar[str]
    supports_history: ClassVar[bool] = False

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured to answer."""

    @abstractmethod
    def forecast_slots(self, latitude: float, longitude: float, *, raw: bool = False) -> list[ForecastSlot] | None:
        """
        Forecast slots, oldest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            raw: The finest-grained slots, unfiltered, for matching against a specific time; otherwise the compact
                near-term strip.

        Returns:
            The slots, or None when this provider declines. An empty strip is also a decline for the compact form; for
            ``raw`` any list is an answer.
        """

    def sun_times(self, latitude: float, longitude: float) -> SunTimes | None:
        """
        Today's sunrise/sunset and golden-hour windows, or None when this provider has none.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
        """
        return None

    def historical_days(self, latitude: float, longitude: float, start: date, end: date) -> dict[str, dict[str, Any]]:
        """
        Recorded weather per day, keyed by ISO date; empty when the provider could not answer.

        Only called when :attr:`supports_history`. A record carries ``temperature_max_c``/``temperature_min_c``/
        ``temperature_mean_c``, ``precipitation_mm``, ``snowfall_cm``, ``wind_speed_max_kmh`` and ``wind_gusts_max_kmh``.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            start: First day, inclusive.
            end: Last day, inclusive.
        """
        return {}


class OpenWeatherMapProvider(WeatherProvider):
    """OpenWeatherMap, when a key is configured."""

    key: ClassVar[str] = "openweathermap"

    def available(self) -> bool:
        """Whether the OpenWeatherMap key is set."""
        return bool(settings.openweathermap_api_key)

    def forecast_slots(self, latitude: float, longitude: float, *, raw: bool = False) -> list[ForecastSlot] | None:
        """The 5-day forecast, or the raw entries; None on failure."""
        from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

        try:
            gateway = OpenWeatherMapGateway()
            raw_forecast = gateway.get_raw_forecast(latitude, longitude) if raw else gateway.get_weather_forecast(latitude, longitude)
        except Exception:
            logger.warning("OpenWeatherMap %sforecast failed, falling back to Open-Meteo", "raw " if raw else "", exc_info=True)
            return None
        if not raw_forecast:
            return None
        return [slot for item in raw_forecast if (slot := owm_item_to_slot(item)) is not None]


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo, which needs no key."""

    key: ClassVar[str] = "open_meteo"

    def available(self) -> bool:
        """Always."""
        return True

    def forecast_slots(self, latitude: float, longitude: float, *, raw: bool = False) -> list[ForecastSlot] | None:
        """The forecast; the same slots serve the strip and the raw form."""
        from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

        return OpenMeteoGateway().get_weather_forecast(latitude, longitude) or []

    def sun_times(self, latitude: float, longitude: float) -> SunTimes | None:
        """Today's sun times."""
        from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

        return OpenMeteoGateway().get_sun_times(latitude, longitude)


def weather_providers() -> list[WeatherProvider]:
    """Every contributed provider that is available, in priority order."""
    from urbanlens.dashboard.plugins.points import WEATHER_PROVIDERS

    return [provider for provider in WEATHER_PROVIDERS if provider.available()]


def history_providers() -> list[WeatherProvider]:
    """The available providers that can report recorded weather."""
    return [provider for provider in weather_providers() if provider.supports_history]


def get_forecast_slots(latitude: float, longitude: float) -> list[ForecastSlot]:
    """
    Return a forecast strip for a coordinate, trying each available provider in priority order.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Normalized ``ForecastSlot`` entries, oldest first - empty when every provider has nothing."""
    for provider in weather_providers():
        slots = provider.forecast_slots(latitude, longitude)
        if slots:
            return slots
    return []


def get_raw_forecast_slots(latitude: float, longitude: float) -> list[ForecastSlot]:
    """
    Return the finest-grained forecast slots available, for matching against a specific time.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Normalized ``ForecastSlot`` entries, unfiltered, oldest first."""
    for provider in weather_providers():
        slots = provider.forecast_slots(latitude, longitude, raw=True)
        if slots is not None:
            return slots
    return []


def get_sun_times(latitude: float, longitude: float) -> SunTimes | None:
    """
    Return today's sunrise/sunset and golden-hour windows from the first provider that has them.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Today's sun times, or None when unavailable everywhere."""
    for provider in weather_providers():
        sun_times = provider.sun_times(latitude, longitude)
        if sun_times is not None:
            return sun_times
    return None
