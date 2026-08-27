"""Gateways for REData's weather endpoints - the forecast, and the record.

See ``../REData/docs/api-reference.md``, "GET /weather/ - conditions, forecast
and sun times for a point". Every registered provider (``open_meteo``,
``openweathermap``) answers in one call, each as its own entry in the
near-a-coordinate envelope's ``results`` - REData never merges them, since
they publish different capabilities (e.g. OpenWeatherMap's ``sun`` is always
``{}``). ``services.apis.locations.weather_resolution`` picks which entry to
use for which purpose.

``GET /weather/history/`` is its counterpart and the opposite kind of fact: a
forecast is only meaningful relative to when it was made, while a record of a
day that has already happened never changes. It gets its own gateway rather
than a second method here because the two answer different questions and are
rate-limited, cached and surfaced separately.
"""

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

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            One entry per provider that answered - each a dict with
            ``provider``, ``current``, ``forecast``, ``sun`` keys (REData's
            own shape - see the module docstring). Empty when every
            registered provider failed to cover the coordinate, which
            shouldn't happen in practice (weather covers everywhere) but is
            handled the same as "REData found nothing" by callers.

        Raises:
            LocationContextUnavailableError: A total blackout (every source
                failed), a REData-side validation error, or the request
                itself failed outright.
        """
        envelope = self.near_point("/api/v1/weather/", latitude, longitude)
        return envelope.results


@dataclass(slots=True, kw_only=True)
class RedataWeatherHistoryGateway(RedataLocationContextGateway):
    """REST client for REData's historical (ERA5 reanalysis) weather endpoint."""

    service_key: ClassVar[str] = "redata_weather_history"

    def get_history(self, latitude: float, longitude: float, *, start: date, end: date) -> list[dict[str, Any]]:
        """Fetch one recorded day's weather per day in a date range.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            start: First day to ask for, inclusive.
            end: Last day to ask for, inclusive.

        Returns:
            One dict per day REData could answer for, each carrying its own
            ``date`` plus ``temperature_max_c``/``temperature_min_c``/
            ``temperature_mean_c``, ``precipitation_mm``, ``snowfall_cm``,
            ``wind_speed_max_kmh`` and ``wind_gusts_max_kmh``. Units are fixed
            by REData (Celsius, millimetres, centimetres, km/h); a null is a
            real answer, since ERA5 gained some variables later than others.

            The range is **clamped, not rejected** - ERA5 starts in 1940 and
            lags real time by about six days - so a request spanning either
            edge returns the days that exist rather than failing, and an
            entirely future range returns nothing. Match rows by their own
            ``date``; do not assume one row per day asked for.

        Raises:
            LocationContextUnavailableError: The source was unavailable or
                rate-limited, REData rejected the parameters, or the request
                itself failed.
        """
        envelope = self.near_point(
            "/api/v1/weather/history/",
            latitude,
            longitude,
            extra_params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        return envelope.results
