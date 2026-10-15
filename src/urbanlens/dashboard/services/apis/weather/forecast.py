"""Provider-agnostic weather forecast shape, shared by every weather provider."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict


class ForecastSlot(TypedDict):
    """
    One forecast time slot, in a shape independent of the source provider.
    Time arithmetic (matching a slot to a scheduled activity, bucketing by day) must use ``date_utc`` instead."""

    #: Naive **UTC**, so slots from different providers are comparable and can be matched against
    #: `Activity.scheduled_at` (also stored UTC).
    #: Providers that report local time convert on the way out - see `OpenMeteoGateway`, which
    #: requests `timezone=auto` for its own local-time behaviour and subtracts the
    date: datetime
    #: The slot's instant as an aware UTC datetime - see the class docstring
    #: for the per-provider derivation and when it can be absent.
    date_utc: NotRequired[datetime]
    temp: float
    condition: str
    icon: str
    humidity: int | None
    wind_speed: float | None
    #: "Feels like" temperature - absent (rather than equal to ``temp``) when
    #: the source doesn't publish one.
    feels_like: NotRequired[float | None]
    #: 0-100 chance of precipitation - absent when the source doesn't publish one.
    precipitation_probability: NotRequired[int | None]


class SunTimes(TypedDict):
    """Sunrise/sunset and golden-hour windows for one local calendar day."""

    sunrise: datetime
    sunset: datetime
    golden_hour_morning_end: datetime
    golden_hour_evening_start: datetime


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
    """
    Convert one raw OpenWeatherMap forecast entry into a ``ForecastSlot``.

    Args:
        item: One entry of OpenWeatherMap's ``list`` response, already carrying a parsed ``date`` field (see ``OpenWeatherMapGateway._parse_dates``).

    Returns:
        The normalized slot, or None when the entry is missing its date."""
    date = item.get("date")
    if not isinstance(date, datetime):
        return None
    main = (item.get("main") or {}) if isinstance(item.get("main"), dict) else {}
    weather = (item.get("weather") or [{}])[0] if item.get("weather") else {}
    wind = (item.get("wind") or {}) if isinstance(item.get("wind"), dict) else {}
    condition = weather.get("main") or "Unknown"
    pop = item.get("pop")
    return ForecastSlot(
        date=date,
        date_utc=date.astimezone(UTC) if date.tzinfo is not None else date.replace(tzinfo=UTC),
        temp=float(main.get("temp") or 0.0),
        condition=condition,
        icon=_OWM_CONDITION_ICONS.get(condition, "cloud"),
        humidity=main.get("humidity"),
        wind_speed=wind.get("speed"),
        feels_like=main.get("feels_like"),
        precipitation_probability=round(pop * 100) if isinstance(pop, (int, float)) else None,
    )
