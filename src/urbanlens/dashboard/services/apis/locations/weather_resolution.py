"""Resolves weather/sun-times lookups to REData or direct providers, per call.

Single chokepoint for the "which provider answers a weather call" decision,
mirroring ``cid_resolution.py``/``places_resolution.py``'s precedent for the
same REData-vs-direct choice:

- REData configured (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` both set) -
  the primary deployment's path. One call to REData's ``GET /weather/``
  returns every registered provider's (``open_meteo``, ``openweathermap``)
  current/forecast/sun data at once - see ``redata_weather_gateway``.
- REData not configured, or its request fails - falls back to the existing
  direct chain: OpenWeatherMap first when a key is configured, then the free,
  keyless Open-Meteo gateway, preserving each call site's current behavior
  exactly.

Every path converts into the same ``ForecastSlot``/``SunTimes`` shapes
(``services.apis.weather.forecast``), so callers never branch on which
provider actually answered.
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_weather_gateway import RedataWeatherGateway
from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot, SunTimes, owm_item_to_slot, redata_forecast_to_slots, redata_sun_to_sun_times
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: Preference order for picking one REData provider entry when a call site
#: only wants a single answer (matches today's "OpenWeatherMap first" rule
#: for the forecast strip, and "always Open-Meteo" for sun times - see below).
_FORECAST_PROVIDER_PREFERENCE = ("openweathermap", "open_meteo")
_SUN_PROVIDER_PREFERENCE = ("open_meteo", "openweathermap")


def _redata_results(latitude: float, longitude: float) -> list[dict] | None:
    """Fetch REData's weather results, or None if REData isn't configured/fails."""
    if not redata_configured():
        return None
    try:
        return RedataWeatherGateway().get_weather(latitude, longitude)
    except LocationContextUnavailableError as exc:
        logger.warning("REData weather lookup failed for the given coordinates, falling back to direct providers: %s", exc)
        return None


def _pick_entry(results: list[dict], preference: tuple[str, ...], *, key: str) -> dict | None:
    """Return the first preferred provider's entry whose ``key`` block is non-empty."""
    by_provider = {entry.get("provider"): entry for entry in results}
    for provider in preference:
        entry = by_provider.get(provider)
        if entry and entry.get(key):
            return entry
    for entry in results:
        if entry.get(key):
            return entry
    return None


def get_forecast_slots(latitude: float, longitude: float) -> list[ForecastSlot]:
    """Return a forecast strip for a coordinate, trying REData then the direct provider chain.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Normalized ``ForecastSlot`` entries, oldest first - empty when every
        provider (REData included) has nothing.
    """
    results = _redata_results(latitude, longitude)
    if results is not None:
        entry = _pick_entry(results, _FORECAST_PROVIDER_PREFERENCE, key="forecast")
        if entry is not None:
            # Near-term hourly slots read as "today/tomorrow's weather" the
            # way the widget is meant to - REData's further-out daily slots
            # are for get_raw_forecast_slots' longer-range trip matching below,
            # not this compact strip.
            hourly = [item for item in (entry.get("forecast") or []) if item.get("granularity") == "hourly"]
            slots = redata_forecast_to_slots(hourly or entry.get("forecast") or [])
            if slots:
                return slots

    if settings.openweathermap_api_key:
        from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

        try:
            raw_forecast = OpenWeatherMapGateway().get_weather_forecast(latitude, longitude)
        except Exception:
            logger.warning("OpenWeatherMap forecast failed, falling back to Open-Meteo", exc_info=True)
            raw_forecast = None
        if raw_forecast:
            slots = [slot for item in raw_forecast if (slot := owm_item_to_slot(item)) is not None]
            if slots:
                return slots

    from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

    return OpenMeteoGateway().get_weather_forecast(latitude, longitude) or []


def get_raw_forecast_slots(latitude: float, longitude: float) -> list[ForecastSlot]:
    """Return the finest-grained forecast slots available, for matching against a specific time.

    Unlike :func:`get_forecast_slots` (which OpenWeatherMap's own path filters
    to a morning/evening strip - see ``OpenWeatherMapGateway.filter_forecast``),
    this returns every slot REData/OpenWeatherMap published, so a caller can
    find the single closest slot to an arbitrary timestamp (e.g. a trip
    activity's scheduled time - see ``controllers.trip._build_activity_forecasts``).

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Normalized ``ForecastSlot`` entries, unfiltered, oldest first.
    """
    results = _redata_results(latitude, longitude)
    if results is not None:
        entry = _pick_entry(results, _FORECAST_PROVIDER_PREFERENCE, key="forecast")
        if entry is not None:
            return redata_forecast_to_slots(entry.get("forecast") or [])

    if settings.openweathermap_api_key:
        from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

        try:
            raw_forecast = OpenWeatherMapGateway().get_raw_forecast(latitude, longitude)
        except Exception:
            logger.warning("OpenWeatherMap raw forecast failed, falling back to Open-Meteo", exc_info=True)
            raw_forecast = None
        if raw_forecast:
            return [slot for item in raw_forecast if (slot := owm_item_to_slot(item)) is not None]

    from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

    return OpenMeteoGateway().get_weather_forecast(latitude, longitude) or []


def get_sun_times(latitude: float, longitude: float) -> SunTimes | None:
    """Return today's sunrise/sunset and golden-hour windows, trying REData then Open-Meteo.

    Always prefers Open-Meteo's own answer (UL-345: OpenWeatherMap's 5-day/
    3-hour endpoint has no sunrise/sunset field), independent of which
    provider serves the temperature/condition forecast above.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Today's sun times, or None when unavailable everywhere.
    """
    results = _redata_results(latitude, longitude)
    if results is not None:
        entry = _pick_entry(results, _SUN_PROVIDER_PREFERENCE, key="sun")
        if entry is not None:
            sun_times = redata_sun_to_sun_times(entry.get("sun") or {})
            if sun_times is not None:
                return sun_times

    from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

    return OpenMeteoGateway().get_sun_times(latitude, longitude)


__all__ = [
    "get_forecast_slots",
    "get_raw_forecast_slots",
    "get_sun_times",
]
