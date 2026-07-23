"""One suggestion, offered once per pin: organize this property's hierarchy.

Pinning a campus is one click; modelling it properly is a hundred more. And a
user who pinned those buildings individually - before child pins existed, or
via a bulk import - has them all sitting at the top level of their map when
they belong under one property. Both are offered together the first time the
owner opens the pin's detail page, as a single dialog with a single yes.

Endpoints, all pin-scoped:

- ``offer``   - the dialog, or a quiet 204 when there's nothing to suggest.
  Self-polls while the parcel's building lookup is still in flight, so nothing
  ever waits on REData.
- ``apply``   - do it: create the missing building pins and nest the matching
  top-level pins.
- ``dismiss`` - "No" (this pin, permanently) or "Don't show again" (turns the
  owner's ``suggest_pin_restructure`` setting off for every pin at once).
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services import pin_restructure
from urbanlens.dashboard.services.locations import site_scope

logger = logging.getLogger(__name__)


def _poll_attempt(request: HttpRequest) -> int:
    """Which poll cycle this request is (0 for the initial load).

    Mirrors ``PinController._poll_attempt`` - the suggestion shares the panel
    machinery's poll budget without being a panel itself.

    Args:
        request: The current request.

    Returns:
        The attempt number, never negative.
    """
    try:
        return max(int(request.GET.get("attempt", "0")), 0)
    except (TypeError, ValueError):
        return 0


def _toast(response: HttpResponse, level: str, message: str, *, refresh: bool = False) -> HttpResponse:
    """Attach a toast (and optionally a refresh event) to an HTMX response."""
    triggers: dict = {"showToast": {"level": level, "message": message}}
    if refresh:
        triggers["pinDetailPinsChanged"] = True
    response["HX-Trigger"] = json.dumps(triggers)
    return response


class PinRestructureOfferView(LoginRequiredMixin, View):
    """GET: the restructure suggestion for this pin, or a quiet 204."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        if not pin_restructure.should_offer(pin):
            return HttpResponse(status=204)

        if site_scope.parcel_buildings(pin.location) is None:
            # The parcel's buildings have never been looked up. Schedule it and
            # poll, rather than making the page wait on a REData round-trip -
            # the nesting half of the suggestion is worth waiting for it too,
            # since a plan showing only half of what it will do reads as a bug.
            attempt = _poll_attempt(request)
            if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(site_scope.PARCEL_BUILDINGS_CACHE_SOURCE, pin):
                return self._render_plan(request, pin)
            return render(
                request,
                "dashboard/partials/pins/_pin_restructure_pending.html",
                {"poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS},
            )

        return self._render_plan(request, pin)

    @staticmethod
    def _render_plan(request: HttpRequest, pin: Pin) -> HttpResponse:
        """Render the dialog for whatever this pin's plan turns out to be."""
        plan = pin_restructure.plan_for(pin)
        if plan.is_empty:
            return HttpResponse(status=204)
        return render(
            request,
            "dashboard/partials/pins/_pin_restructure_offer.html",
            {
                "pin": pin,
                "buildings": plan.buildings,
                "building_count": len(plan.buildings),
                "nestable": plan.nestable[:5],
                "nestable_count": len(plan.nestable),
                "sample_names": [name for building in plan.buildings[:3] if (name := pin_restructure.building_name(building))],
            },
        )


class PinRestructureDismissView(LoginRequiredMixin, View):
    """POST: decline the suggestion, for this pin or for every pin.

    ``?scope=all`` is the dialog's "Don't show again" - it turns the owner's
    ``suggest_pin_restructure`` setting off, so the suggestion stops appearing
    everywhere, and still marks this pin so the setting can be turned back on
    later without this particular pin re-asking.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("profile"), slug=pin_slug, profile__user=request.user)
        pin.restructure_offer_dismissed = True
        pin.save(update_fields=["restructure_offer_dismissed", "updated"])

        response = HttpResponse("", status=200)
        if request.GET.get("scope") == "all":
            profile = pin.profile
            profile.suggest_pin_restructure = False
            profile.save(update_fields=["suggest_pin_restructure"])
            return _toast(response, "info", "Pin organization suggestions turned off. You can turn them back on in Settings → Map.")
        return response


class PinRestructureApplyView(LoginRequiredMixin, View):
    """POST: create the missing building pins and nest the matching top-level pins.

    Idempotent in the way that matters: the plan is recomputed here rather than
    trusted from the page, so buildings pinned (or pins nested) since the dialog
    rendered are simply skipped.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        plan = pin_restructure.plan_for(pin)
        if plan.is_empty:
            return _toast(HttpResponse("", status=200), "info", "This property is already organized.")

        created = pin_restructure.create_building_pins(pin, plan.buildings)
        wiki_created = pin_restructure.mirror_buildings_to_wiki(pin, plan.buildings, pin.profile)
        nested = pin_restructure.nest_root_pins(pin, plan.nestable)

        parts = []
        if created:
            parts.append(f"Added {created} building pin{'s' if created != 1 else ''}.")
        if nested:
            parts.append(f"Nested {nested} existing pin{'s' if nested != 1 else ''} under this property.")
        if wiki_created:
            parts.append(f"{wiki_created} added to the community wiki.")
        return _toast(HttpResponse("", status=200), "success", " ".join(parts) or "Nothing left to organize.", refresh=True)


class PinBuildingImportView(LoginRequiredMixin, View):
    """POST: create a child pin for every unpinned building on this property.

    The "Buildings on this Property" panel's own action, distinct from the
    restructure suggestion above: always available (never dismissed), and
    scoped strictly to buildings - it never re-parents anything.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)

        missing = pin_restructure.missing_buildings(pin)
        if not missing:
            return _toast(HttpResponse("", status=200), "info", "Every building here already has a pin.")

        created = pin_restructure.create_building_pins(pin, missing)
        wiki_created = pin_restructure.mirror_buildings_to_wiki(pin, missing, pin.profile)

        message = f"Added {created} building pin{'s' if created != 1 else ''}."
        if wiki_created:
            message += f" {wiki_created} added to the community wiki."
        return _toast(HttpResponse("", status=200), "success", message, refresh=True)
