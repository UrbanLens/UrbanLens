"""The assistant's weather tool - calls OpenWeatherMap/Open-Meteo directly, never REData.

Mirrors ``services.apis.locations.weather_resolution``'s own OpenWeatherMap-
then-Open-Meteo direct fallback chain, minus its REData-first branch: this
tool must not depend on REData being configured or reachable at all (see
``docs/AI_PIPELINE.md``'s "no REData" guarantee for the sandboxed AI worker).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register, remaining_deadline

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot


def _resolve_point(context: ToolContext, pin_slug: str, lat: float | None, lng: float | None) -> tuple[float, float] | None:
    """A ``(lat, lng)`` point from one of the requesting profile's own pins, or an explicit coordinate."""
    pin_slug = pin_slug.strip()
    if pin_slug:
        from urbanlens.dashboard.models.pin.model import Pin

        pin = Pin.objects.by_profile(context.profile).filter(slug=pin_slug).select_related("location").first()
        if pin is None or pin.location is None:
            return None
        return float(pin.location.latitude), float(pin.location.longitude)
    if lat is not None and lng is not None:
        return lat, lng
    return None


def _fetch_slot(latitude: float, longitude: float, context: ToolContext) -> tuple[ForecastSlot, str] | None:
    """The nearest upcoming forecast slot, trying OpenWeatherMap (if configured) then the free Open-Meteo."""
    from urbanlens.dashboard.services.apis.weather.forecast import owm_item_to_slot
    from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway
    from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway
    from urbanlens.dashboard.services.core.timeout_utils import call_with_deadline
    from urbanlens.UrbanLens.settings.app import settings

    if settings.openweathermap_api_key:
        try:
            raw = call_with_deadline(lambda: OpenWeatherMapGateway().get_weather_forecast(latitude, longitude), timeout=remaining_deadline(context), default=None, name="openweathermap")
        except Exception:
            raw = None
        if raw:
            slot = owm_item_to_slot(raw[0])
            if slot is not None:
                return slot, "openweathermap"

    slots = call_with_deadline(lambda: OpenMeteoGateway().get_weather_forecast(latitude, longitude), timeout=remaining_deadline(context), default=None, name="open_meteo")
    if slots:
        return slots[0], "open_meteo"
    return None


class GetWeatherArgs(BaseModel):
    pin_slug: str = Field(default="", max_length=255)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


def _get_weather(context: ToolContext, args: GetWeatherArgs) -> dict[str, Any]:
    point = _resolve_point(context, args.pin_slug, args.lat, args.lng)
    if point is None:
        return {"error": "Need either one of the user's own pins (pin_slug) or explicit lat/lng coordinates."}

    found = _fetch_slot(*point, context)
    if found is None:
        return {"source": "unavailable"}
    slot, source = found
    return {
        "source": source,
        "temp_f": round(slot["temp"]),
        "condition": slot["condition"],
        # The nearest forecast slot, not necessarily right now - both providers
        # publish twice-daily (09:00/18:00) or 3-hourly slots, never a live
        # observation, so the model must say when this is for, not "currently".
        "at": slot["date"].isoformat(),
    }


register(
    ToolSpec(
        name="get_weather",
        description=(
            "Weather forecast for one of the user's own pins (pin_slug) or explicit coordinates (lat/lng). Returns the nearest "
            "upcoming forecast slot ('at'), not a live observation - phrase the answer around that time, never as 'right now'. "
            "source is 'unavailable' when no provider answered."
        ),
        args_model=GetWeatherArgs,
        handler=_get_weather,
        features=frozenset({SiteFeature.AI}),
        requires_external_apis=True,
        scope=DataScope.OWN_PROFILE,
        progress_label="Checking the weather…",
        action_label="Checked the weather",
    ),
)
