"""Provider-agnostic weather forecast shape, shared by OpenWeatherMap and Open-Meteo.

``weather_forecast`` in ``controllers/pin.py`` tries OpenWeatherMap first (its
existing raw-JSON shape, converted here) and falls back to the free, keyless
Open-Meteo gateway (already normalized) when OpenWeatherMap isn't configured
or its call fails - both render through the same ``weather.html`` markup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict


class ForecastSlot(TypedDict):
    """One forecast time slot, in a shape independent of the source provider."""

    date: datetime
    temp: float
    condition: str
    icon: str
    humidity: int | None
    wind_speed: float | None


#: OpenWeatherMap's coarse ``weather[0].main`` categories, mapped to the same
#: Material Symbols icon names Open-Meteo's WMO-code mapping uses.
_OWM_CONDITION_ICONS: dict[str, str] = {
    "Clear": "wb_sunny",
    "Clouds": "cloud",
    "Drizzle": "rainy",
    "Rain": "rainy",
    "Thunderstorm": "thunderstorm",
    "Snow": "ac_unit",
    "Mist": "foggy",
    "Smoke": "foggy",
    "Haze": "foggy",
    "Fog": "foggy",
    "Sand": "foggy",
    "Dust": "foggy",
    "Ash": "foggy",
    "Squall": "air",
    "Tornado": "air",
}


def owm_item_to_slot(item: dict[str, Any]) -> ForecastSlot | None:
    """Convert one raw OpenWeatherMap forecast entry into a ``ForecastSlot``.

    Args:
        item: One entry of OpenWeatherMap's ``list`` response, already
            carrying a parsed ``date`` field (see
            ``OpenWeatherMapGateway._parse_dates``).

    Returns:
        The normalized slot, or None when the entry is missing its date.
    """
    date = item.get("date")
    if not isinstance(date, datetime):
        return None
    main = (item.get("main") or {}) if isinstance(item.get("main"), dict) else {}
    weather = (item.get("weather") or [{}])[0] if item.get("weather") else {}
    wind = (item.get("wind") or {}) if isinstance(item.get("wind"), dict) else {}
    condition = weather.get("main") or "Unknown"
    return ForecastSlot(
        date=date,
        temp=float(main.get("temp") or 0.0),
        condition=condition,
        icon=_OWM_CONDITION_ICONS.get(condition, "cloud"),
        humidity=main.get("humidity"),
        wind_speed=wind.get("speed"),
    )
