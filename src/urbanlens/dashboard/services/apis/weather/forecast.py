"""Provider-agnostic weather forecast shape, shared by REData, OpenWeatherMap, and Open-Meteo.

``services.apis.locations.weather_resolution`` tries REData first (when
configured) and falls back to OpenWeatherMap-then-Open-Meteo otherwise - every
path converts into the same ``ForecastSlot``/``SunTimes`` shapes here, so
``weather.html``/``trip_weather.html`` render one markup regardless of source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NotRequired, TypedDict


class ForecastSlot(TypedDict):
    """One forecast time slot, in a shape independent of the source provider."""

    date: datetime
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
    """Sunrise/sunset and golden-hour windows for one local calendar day.

    Golden hour is approximated as the hour immediately after sunrise and
    the hour immediately before sunset - the common photography-app
    convention, rather than a precise solar-elevation calculation (which
    would need a separate astronomy library); good enough for planning
    purposes.
    """

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
    pop = item.get("pop")
    return ForecastSlot(
        date=date,
        temp=float(main.get("temp") or 0.0),
        condition=condition,
        icon=_OWM_CONDITION_ICONS.get(condition, "cloud"),
        humidity=main.get("humidity"),
        wind_speed=wind.get("speed"),
        feels_like=main.get("feels_like"),
        precipitation_probability=round(pop * 100) if isinstance(pop, (int, float)) else None,
    )


#: REData's own closed condition vocabulary (``../REData/docs/api-reference.md``,
#: "GET /weather/" - never a provider's own code), mapped to the same Material
#: Symbols icon names the OpenWeatherMap/Open-Meteo mappings above use.
_REDATA_CONDITION_ICONS: dict[str, str] = {
    "clear": "wb_sunny",
    "mostly_clear": "partly_cloudy_day",
    "partly_cloudy": "partly_cloudy_day",
    "overcast": "cloud",
    "fog": "foggy",
    "drizzle": "rainy",
    "rain": "rainy",
    "freezing_rain": "rainy",
    "snow": "ac_unit",
    "showers": "rainy",
    "snow_showers": "ac_unit",
    "thunderstorm": "thunderstorm",
    "unknown": "cloud",
}


def redata_condition_label(condition: str) -> str:
    """Turn REData's ``snake_case`` condition vocabulary into a display label (``"partly_cloudy" -> "Partly Cloudy"``)."""
    return condition.replace("_", " ").title()


def redata_forecast_to_slots(forecast: list[dict[str, Any]]) -> list[ForecastSlot]:
    """Convert REData's ``weather.forecast`` list into ``ForecastSlot`` entries.

    Args:
        forecast: The ``forecast`` array from one REData weather result entry
            (a mix of ``granularity: "hourly"``/``"daily"`` slots).

    Returns:
        Normalized slots, skipping any entry with no parseable ``starts_at``.
    """
    slots: list[ForecastSlot] = []
    for entry in forecast:
        starts_at = entry.get("starts_at")
        if not starts_at:
            continue
        try:
            date = datetime.fromisoformat(starts_at)
        except (TypeError, ValueError):
            continue
        condition = entry.get("condition") or "unknown"
        temperature_c = entry.get("temperature_c")
        if temperature_c is None:
            temperature_c = entry.get("temperature_max_c")
        precipitation_probability = entry.get("precipitation_probability_percent")
        slots.append(
            ForecastSlot(
                date=date,
                temp=_celsius_to_fahrenheit(temperature_c) if temperature_c is not None else 0.0,
                condition=redata_condition_label(condition),
                icon=_REDATA_CONDITION_ICONS.get(condition, "cloud"),
                humidity=entry.get("relative_humidity_percent"),
                wind_speed=_ms_to_mph(entry.get("wind_speed_ms")),
                feels_like=_optional_celsius_to_fahrenheit(entry.get("apparent_temperature_c")),
                precipitation_probability=round(precipitation_probability) if isinstance(precipitation_probability, (int, float)) else None,
            ),
        )
    return slots


def redata_sun_to_sun_times(sun: dict[str, Any]) -> SunTimes | None:
    """Convert REData's ``weather.sun`` block into a ``SunTimes``.

    Args:
        sun: The ``sun`` object from one REData weather result entry - ``{}``
            for a provider that doesn't publish sun times (see REData's
            ``docs/api-reference.md``, "GET /weather/").

    Returns:
        The normalized sun times, or None when any field is missing/unparseable.
    """
    try:
        return SunTimes(
            sunrise=datetime.fromisoformat(sun["sunrise"]),
            sunset=datetime.fromisoformat(sun["sunset"]),
            golden_hour_morning_end=datetime.fromisoformat(sun["golden_hour_morning_end"]),
            golden_hour_evening_start=datetime.fromisoformat(sun["golden_hour_evening_start"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _celsius_to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def _optional_celsius_to_fahrenheit(celsius: float | None) -> float | None:
    return _celsius_to_fahrenheit(celsius) if celsius is not None else None


def _ms_to_mph(meters_per_second: float | None) -> float | None:
    return meters_per_second * 2.236936 if meters_per_second is not None else None
