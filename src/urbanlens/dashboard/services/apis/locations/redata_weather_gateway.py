"""Gateways for REData's weather endpoints - the forecast, and the record.
Every registered provider (``open_meteo``, ``openweathermap``) answers in one call, each as its own entry in the near-a-coordinate envelope's ``results`` - REData never merges them, since they publish different capabilities (e.g."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

if TYPE_CHECKING:
    from datetime import date


@dataclass(slots=True, kw_only=True)
class RedataWeatherGateway(RedataLocationContextGateway):
    """REST client for REData's weather endpoint."""

    service_key: ClassVar[str] = "redata_weather"

    def get_weather(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """Fetch every registered weather provider's current/forecast/sun data for a point.

        Returns:
            One entry per provider that answered - each a dict with ``provider``, ``current``, ``forecast``, ``sun`` keys (REData's own shape - see the module docstring).

        Raises:
            LocationContextUnavailableError: A total blackout (every source failed), a REData-side validation error, or the request itself failed outright."""
        envelope = self.near_point("/api/v1/weather/", latitude, longitude)
        return envelope.results


@dataclass(slots=True, kw_only=True)
class RedataWeatherHistoryGateway(RedataLocationContextGateway):
    """REST client for REData's historical (ERA5 reanalysis) weather endpoint."""

    service_key: ClassVar[str] = "redata_weather_history"

    def get_history(self, latitude: float, longitude: float, *, start: date, end: date) -> list[dict[str, Any]]:
        """Fetch one recorded day's weather per day in a date range.

        Returns:
            One dict per day REData could answer for, each carrying its own ``date`` plus ``temperature_max_c``/``temperature_min_c``/ ``temperature_mean_c``, ``precipitation_mm``, ``snowfall_cm``, ``wind_speed_max_kmh`` and ``wind_gusts_max_kmh``.

        Raises:
            LocationContextUnavailableError: The source was unavailable or rate-limited, REData rejected the parameters, or the request itself failed."""
        envelope = self.near_point(
            "/api/v1/weather/history/",
            latitude,
            longitude,
            extra_params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        return envelope.results
