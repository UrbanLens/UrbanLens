"""Gateway for REData's ``GET /weather/`` - current conditions, forecast, and sun times.

See ``../REData/docs/api-reference.md``, "GET /weather/ - conditions, forecast
and sun times for a point". Every registered provider (``open_meteo``,
``openweathermap``) answers in one call, each as its own entry in the
near-a-coordinate envelope's ``results`` - REData never merges them, since
they publish different capabilities (e.g. OpenWeatherMap's ``sun`` is always
``{}``). ``services.apis.locations.weather_resolution`` picks which entry to
use for which purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway


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
