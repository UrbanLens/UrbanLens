"""The assistant's visit-history tool - "have I been here?"

Three tiers of confidence, never collapsed into one another: a logged
``PinVisit`` or the "Visited" status label is a *confirmed* visit; a pending
``VisitSuggestion`` or a recorded ``Route`` passing within 150m is evidence the
user was *nearby*, not proof they went in. Reporting the second tier as
confirmed would upgrade a GPS track into a visit the user never actually logged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: How close a recorded GPS track must pass to count as "nearby", matching the
#: rough width of a large property rather than a precise doorway.
_NEARBY_ROUTE_METERS = 150


def _resolve_own_pin(context: ToolContext, pin_slug: str) -> Pin | None:
    """One of the requesting profile's own pins.

    Never resolves any other profile's pin - see ``Pin.objects.by_profile``.
    PinVisit has no profile FK of its own, so every visit lookup below is
    only ever safe because it starts from a pin already scoped this way.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.by_profile(context.profile).filter(slug=pin_slug.strip()).select_related("location").first()


def _confirmed_visit(pin: Pin) -> dict[str, Any] | None:
    from urbanlens.dashboard.models.visits.model import PinVisit

    latest = PinVisit.objects.for_pin(pin.pk).filter(tentative=False).first()
    visit_count = PinVisit.objects.for_pin(pin.pk).filter(tentative=False).count()
    has_visited_label = pin.labels.filter(name="Visited", kind=KIND_STATUS).exists()
    if latest is None and not has_visited_label:
        return None
    return {
        "visit_count": visit_count,
        "last_visited": latest.visited_at.date().isoformat() if latest is not None else None,
    }


def _pending_suggestion(pin: Pin, context: ToolContext) -> bool:
    from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus

    return VisitSuggestion.objects.filter(suggested_to=context.profile, status=VisitSuggestionStatus.PENDING, location=pin.location).exists()


def _nearby_route(pin: Pin, context: ToolContext) -> bool:
    from django.contrib.gis.geos import Point
    from django.contrib.gis.measure import D

    from urbanlens.dashboard.models.routes.model import Route

    point = Point(float(pin.location.longitude), float(pin.location.latitude), srid=4326)
    return Route.objects.for_profile(context.profile).passing_within(point, D(m=_NEARBY_ROUTE_METERS)).exists()


class HaveIBeenHereArgs(BaseModel):
    pin_slug: str = Field(min_length=1, max_length=255)


def _have_i_been_here(context: ToolContext, args: HaveIBeenHereArgs) -> dict[str, Any]:
    pin = _resolve_own_pin(context, args.pin_slug)
    if pin is None:
        return {"error": "pin_slug must be one of the user's own pins."}

    confirmed = _confirmed_visit(pin)
    if confirmed is not None:
        return {"status": "confirmed", **confirmed}

    passed_nearby = _pending_suggestion(pin, context) or _nearby_route(pin, context)
    if passed_nearby:
        return {"status": "passed_nearby"}
    return {"status": "no_evidence"}


register(
    ToolSpec(
        name="have_i_been_here",
        description=(
            "Whether the user has visited one of their own pins (pin_slug). status is 'confirmed' (a logged visit or the "
            "'Visited' label - includes visit_count/last_visited), 'passed_nearby' (a pending visit suggestion or a recorded "
            "GPS track passed close by, not a confirmed visit), or 'no_evidence'. Never present 'passed_nearby' as a visit."
        ),
        args_model=HaveIBeenHereArgs,
        handler=_have_i_been_here,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Checking visit history…",
        action_label="Checked visit history",
    ),
)
