"""Pin-lookup tools: search and unvisited. Both strictly ``profile``-scoped, read-only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Exists, OuterRef, Q
from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: Rows any single pin tool may return - matches the row cap the ported
#: assistant.py handlers already used.
_ROW_LIMIT = 10


def _pin_row(pin: Pin) -> dict[str, Any]:
    location = pin.location
    return {
        "name": pin.effective_name,
        "slug": pin.slug,
        "city": (location.locality or "") if location else "",
        "state": (location.administrative_area_level_1 or "") if location else "",
        "visited": bool(getattr(pin, "has_visit", False)),
    }


class SearchPinsArgs(BaseModel):
    query: str = Field(max_length=200)
    limit: int = Field(default=5, ge=1, le=_ROW_LIMIT)


def _search_pins(context: ToolContext, args: SearchPinsArgs) -> dict[str, Any]:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit

    query = args.query.strip()
    if not query:
        return {"error": "query is required"}
    pins = (
        Pin.objects.filter(profile=context.profile, parent_pin__isnull=True)
        .filter(Q(name__icontains=query) | Q(aliases__name__icontains=query) | Q(location__official_name__icontains=query))
        .annotate(has_visit=Exists(PinVisit.objects.filter(pin=OuterRef("pk"))))
        .select_related("location")
        .distinct()[: args.limit]
    )
    return {"pins": [_pin_row(pin) for pin in pins]}


register(
    ToolSpec(
        name="search_pins",
        description="Search the requesting user's own pins (saved places) by name, alias, or location name.",
        args_model=SearchPinsArgs,
        handler=_search_pins,
        features=frozenset({SiteFeature.AI}),
        # Only "name" - city/state come from geocoding (location.locality /
        # administrative_area_level_1), not free text the user typed.
        user_content_fields=frozenset({"name"}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Searching your pins…",
        action_label="Searched your pins",
    ),
)


class FindUnvisitedPinsArgs(BaseModel):
    state: str = Field(default="", max_length=100)
    limit: int = Field(default=5, ge=1, le=_ROW_LIMIT)


def _find_unvisited_pins(context: ToolContext, args: FindUnvisitedPinsArgs) -> dict[str, Any]:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit

    pins = Pin.objects.filter(profile=context.profile, parent_pin__isnull=True).annotate(has_visit=Exists(PinVisit.objects.filter(pin=OuterRef("pk")))).filter(has_visit=False).select_related("location")
    state = args.state.strip()
    if state:
        pins = pins.filter(location__administrative_area_level_1__iexact=state)
    return {"pins": [_pin_row(pin) for pin in pins[: args.limit]]}


register(
    ToolSpec(
        name="find_unvisited_pins",
        description="List the requesting user's own pins that have no logged visit, optionally filtered by US state.",
        args_model=FindUnvisitedPinsArgs,
        handler=_find_unvisited_pins,
        features=frozenset({SiteFeature.AI}),
        # Only "name" - city/state come from geocoding (location.locality /
        # administrative_area_level_1), not free text the user typed.
        user_content_fields=frozenset({"name"}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Looking up unvisited pins…",
        action_label="Looked up unvisited pins",
    ),
)
