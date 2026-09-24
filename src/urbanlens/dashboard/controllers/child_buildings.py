"""A property's building child pins, shown on the property's Private Pin page when "child pin details" is on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.child_buildings import building_panel_sources, child_building_listing

if TYPE_CHECKING:
    from django.http import HttpRequest


def _own_pin(request: HttpRequest, pin_slug: str) -> Pin | None:
    return Pin.objects.select_related("location", "location__place", "profile").filter(slug=pin_slug, profile__user=request.user).first()


def building_card_context(request: HttpRequest, building: Pin) -> dict:
    """What one building's card renders.

    Args:
        request: The current request, for its viewer.
        building: The building child pin.

    Returns:
        The card's template context.
    """
    from urbanlens.dashboard.services.places.ambiguity import linked_wiki_locations

    wikis = linked_wiki_locations(building, building.profile)
    dates = (("construction", "Built", building.date_built), ("door_front", "Abandoned", building.date_abandoned), ("history", "Last active", building.date_last_active))
    return {
        "building": building,
        "building_facts": [{"icon": icon, "text": f"{label} {value.isoformat()}"} for icon, label, value in dates if value],
        "building_wiki_location": wikis[0] if wikis else None,
        "building_panels": building_panel_sources(request.user),
    }


class PinChildBuildingsView(LoginRequiredMixin, View):
    """The building children of a property, for its page.

    GET /map/pin/<slug>/child-buildings/ renders the whole section; ``?offset=N`` renders only the next page of
    collapsed rows, for "Show more". 204 when the pin has no building children.
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _own_pin(request, pin_slug)
        if pin is None:
            return HttpResponse(status=404)
        try:
            offset = max(int(request.GET.get("offset") or 0), 0)
        except ValueError:
            offset = 0
        listing = child_building_listing(pin, offset=offset)
        if listing is None:
            return HttpResponse(status=204)
        context: dict = {"pin": pin, "listing": listing}
        if offset:
            return render(request, "dashboard/partials/pins/_child_building_rows.html", context)
        if listing.expanded is not None:
            context.update(building_card_context(request, listing.expanded))
        return render(request, "dashboard/partials/pins/_child_buildings_section.html", context)


class PinChildBuildingCardView(LoginRequiredMixin, View):
    """One building child's card, loaded when its collapsed row on the parent's page is opened.

    GET /map/pin/<building slug>/building-card/
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        building = _own_pin(request, pin_slug)
        if building is None:
            return HttpResponse(status=404)
        return render(request, "dashboard/partials/pins/_child_building_card.html", building_card_context(request, building))
