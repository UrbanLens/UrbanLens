"""Open-Meteo gateway - free, keyless weather forecast API.

https://open-meteo.com/ - a free, open-source-friendly weather API requiring
no API key, unlike the existing OpenWeatherMap integration. Used as an
automatic fallback when OpenWeatherMap isn't configured or its call fails,
and normalizes to the same provider-agnostic forecast shape OpenWeatherMap's
gateway also produces (see ``ForecastSlot``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: WMO weather-interpretation codes (used by Open-Meteo and several other
#: providers) mapped to a Material Symbols icon name and short label.
_WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("wb_sunny", "Clear"),
    1: ("partly_cloudy_day", "Mostly Clear"),
    2: ("partly_cloudy_day", "Partly Cloudy"),
    3: ("cloud", "Overcast"),
    45: ("foggy", "Fog"),
    48: ("foggy", "Depositing Rime Fog"),
    51: ("rainy", "Light Drizzle"),
    53: ("rainy", "Drizzle"),
    55: ("rainy", "Dense Drizzle"),
    56: ("rainy", "Light Freezing Drizzle"),
    57: ("rainy", "Freezing Drizzle"),
    61: ("rainy", "Slight Rain"),
    63: ("rainy", "Rain"),
    65: ("rainy", "Heavy Rain"),
    66: ("rainy", "Light Freezing Rain"),
    67: ("rainy", "Freezing Rain"),
    71: ("ac_unit", "Slight Snow"),
    73: ("ac_unit", "Snow"),
    75: ("ac_unit", "Heavy Snow"),
    77: ("ac_unit", "Snow Grains"),
    80: ("rainy", "Slight Showers"),
    81: ("rainy", "Showers"),
    82: ("rainy", "Violent Showers"),
    85: ("ac_unit", "Slight Snow Showers"),
    86: ("ac_unit", "Heavy Snow Showers"),
    95: ("thunderstorm", "Thunderstorm"),
    96: ("thunderstorm", "Thunderstorm with Hail"),
    99: ("thunderstorm", "Thunderstorm with Heavy Hail"),
}
_DEFAULT_WMO = ("cloud", "Unknown")


def wmo_code_to_icon_and_label(code: int) -> tuple[str, str]:
    """Map a WMO weather-interpretation code to a Material Symbols icon name and label."""
    return _WMO_CODES.get(code, _DEFAULT_WMO)


@dataclass(slots=True, kw_only=True)
class OpenMeteoGateway(Gateway):
    """Gateway for the free, keyless Open-Meteo forecast API."""

    service_key: ClassVar[str] = "open_meteo"
    paid_service: ClassVar[bool] = False

    def get_weather_forecast(self, latitude: float, longitude: float) -> list[ForecastSlot] | None:
        """Return a morning/evening forecast strip for the next few days.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Normalized ``ForecastSlot`` entries (09:00 and 18:00 local time
            for each of the next 5 days), or None on failure.
        """
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m,weathercode,relative_humidity_2m,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 5,
            "timezone": "auto",
        }
        try:
            response = self.session.get(_FORECAST_URL, params=params, timeout=15)
            response.raise_for_status()
            hourly = response.json().get("hourly") or {}
        except Exception:
            logger.warning("Open-Meteo forecast unavailable for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return None

        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        codes = hourly.get("weathercode") or []
        humidity = hourly.get("relative_humidity_2m") or []
        wind = hourly.get("wind_speed_10m") or []

        slots: list[ForecastSlot] = []
        for index, time_str in enumerate(times):
            if not time_str.endswith(("T09:00", "T18:00")):
                continue
            try:
                date = datetime.fromisoformat(time_str)
            except ValueError:
                continue
            icon, label = wmo_code_to_icon_and_label(int(codes[index])) if index < len(codes) else _DEFAULT_WMO
            slots.append(
                ForecastSlot(
                    date=date,
                    temp=float(temps[index]) if index < len(temps) else 0.0,
                    condition=label,
                    icon=icon,
                    humidity=int(humidity[index]) if index < len(humidity) else None,
                    wind_speed=float(wind[index]) if index < len(wind) else None,
                ),
            )
        return slots
