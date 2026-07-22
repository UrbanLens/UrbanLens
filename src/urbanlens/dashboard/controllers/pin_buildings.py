"""Bulk-create a pin's sub-pins from the buildings known to stand on its property.

Pinning a campus, a mill complex, or a state hospital is one click on the map,
and modelling it properly is a hundred more - one child pin per building,
each placed by hand. The buildings are already known (see
``plugins.builtin.parcel_buildings``), so this offers to place them.

Three endpoints, all pin-scoped:

- ``offer``  - the prompt itself, or a quiet 204 when there is nothing to offer.
  Self-polls while the buildings lookup is still in flight, so pin creation
  never blocks on REData.
- ``dismiss`` - the owner said no; never ask again for this pin.
- ``import``  - create the missing child pins (and, when the place already has
  a community wiki, the matching child wikis).
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations import site_scope

logger = logging.getLogger(__name__)

#: Cap on how many child pins one import call will create. Far above any real
#: parcel (the largest campus this was built for has ~100 buildings), purely a
#: backstop against a provider returning something pathological.
MAX_IMPORT_BUILDINGS = 500


def _poll_attempt(request: HttpRequest) -> int:
    """Which poll cycle this request is (0 for the initial load).

    Mirrors ``PinController._poll_attempt`` - the offer shares the panel
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


def _building_name(building: dict) -> str:
    """A usable pin name for a building record.

    Args:
        building: A cached building record.

    Returns:
        The building's own name, else "Building <number>", else "" (which
        leaves the pin unnamed, falling back to its location's display name
        exactly like any other nameless pin).
    """
    name = (building.get("name") or "").strip()
    if name:
        return name
    number = str(building.get("building_number") or "").strip()
    return f"Building {number}" if number else ""


def _unpinned_buildings(pin: Pin) -> list[dict]:
    """Buildings on this pin's parcel that no child pin covers yet.

    Args:
        pin: The parent pin.

    Returns:
        Building records with usable coordinates and no existing child pin
        within ``BUILDING_MATCH_METERS``.
    """
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import match_child_marker

    buildings = site_scope.parcel_buildings(pin.location) or []
    unmatched = list(pin.detail_pins.select_related("location"))
    missing: list[dict] = []
    for building in buildings:
        if building.get("latitude") is None or building.get("longitude") is None:
            continue
        child = match_child_marker(building, unmatched)
        if child is not None:
            unmatched.remove(child)
            continue
        missing.append(building)
    return missing


class PinBuildingOfferView(LoginRequiredMixin, View):
    """GET: "this property has N buildings - add pins for them?", or a quiet 204."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        if pin.buildings_offer_dismissed or pin.parent_pin_id is not None:
            return HttpResponse(status=204)
        # Already modelled - the user has done this, by hand or by accepting a
        # previous offer, and re-offering would just be noise.
        if site_scope.building_child_count(pin) >= site_scope.MULTI_BUILDING_THRESHOLD:
            return HttpResponse(status=204)

        buildings = site_scope.parcel_buildings(pin.location)
        if buildings is None:
            # Never looked up. Schedule it and poll, rather than making pin
            # creation wait on a REData round-trip it may not even need.
            attempt = _poll_attempt(request)
            if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(site_scope.PARCEL_BUILDINGS_CACHE_SOURCE, pin):
                return HttpResponse(status=204)
            return render(
                request,
                "dashboard/partials/pins/_buildings_offer_pending.html",
                {"poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS},
            )

        missing = _unpinned_buildings(pin)
        if len(missing) < site_scope.MULTI_BUILDING_THRESHOLD:
            return HttpResponse(status=204)

        return render(
            request,
            "dashboard/partials/pins/_buildings_offer.html",
            {"pin": pin, "building_count": len(missing), "sample_names": [name for b in missing[:3] if (name := _building_name(b))]},
        )


class PinBuildingDismissView(LoginRequiredMixin, View):
    """POST: record that the owner declined the buildings offer for this pin."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        pin.buildings_offer_dismissed = True
        pin.save(update_fields=["buildings_offer_dismissed", "updated"])
        return HttpResponse("", status=200)


class PinBuildingImportView(LoginRequiredMixin, View):
    """POST: create one child pin per building on this pin's property.

    Idempotent in the way that matters: buildings already covered by a child
    pin are skipped, so running it twice (or after adding a few pins by hand)
    only ever fills the gaps.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)

        missing = _unpinned_buildings(pin)[:MAX_IMPORT_BUILDINGS]
        if not missing:
            return self._toast(HttpResponse("", status=200), "info", "Every building here already has a pin.")

        created = self._create_child_pins(pin, missing)
        wiki_created = self._mirror_to_wiki(pin, missing, request)

        message = f"Added {created} building pin{'s' if created != 1 else ''}."
        if wiki_created:
            message += f" {wiki_created} added to the community wiki."
        response = HttpResponse("", status=200)
        return self._toast(response, "success", message, refresh=True)

    @staticmethod
    def _create_child_pins(pin: Pin, buildings: list[dict]) -> int:
        """Create a child pin for each building, in one transaction.

        Args:
            pin: The parent pin.
            buildings: Building records to create pins for.

        Returns:
            How many child pins were created.
        """
        from urbanlens.dashboard.controllers.detail_pins import _location_for_coords

        created = 0
        with transaction.atomic():
            for building in buildings:
                name = _building_name(building)
                Pin.objects.create(
                    name=name or None,
                    # Derived from a building record, not typed by the user, so
                    # external name refreshes may still improve it later.
                    name_is_user_provided=False,
                    pin_type=PinType.BUILDING,
                    # Likewise derived: the coordinate came from a building
                    # footprint, so this is exactly the conclusion the
                    # classifier would have reached on its own - no need to
                    # queue one task per building to re-derive it.
                    pin_type_is_user_provided=False,
                    parent_pin=pin,
                    profile=pin.profile,
                    location=_location_for_coords(building["latitude"], building["longitude"]),
                )
                created += 1
        return created

    @staticmethod
    def _mirror_to_wiki(pin: Pin, buildings: list[dict], request: HttpRequest) -> int:
        """Mirror the imported buildings as child wikis, when the place has a wiki.

        Never creates a wiki - community pages are only ever created
        explicitly (see ``services.locations.creation.WikiCreationService``).
        When one already exists, though, its readers benefit from the same
        building markers, so they are contributed there too.

        Args:
            pin: The parent pin, whose location's wiki is the parent wiki.
            buildings: The building records just imported.
            request: The current request, for attributing the WikiEdit.

        Returns:
            How many child wikis were created.
        """
        from django.core.exceptions import ObjectDoesNotExist

        from urbanlens.dashboard.controllers.detail_pins import _location_for_child_wiki
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import match_child_marker

        try:
            wiki = pin.location.wiki
        except ObjectDoesNotExist:
            return 0

        unmatched = list(wiki.child_wikis.select_related("location"))
        created = 0
        with transaction.atomic():
            for building in buildings:
                existing = match_child_marker(building, unmatched)
                if existing is not None:
                    unmatched.remove(existing)
                    continue
                Wiki.objects.create(
                    name=_building_name(building) or wiki.name,
                    pin_type=PinType.BUILDING,
                    pin_type_is_user_provided=False,
                    parent_wiki=wiki,
                    location=_location_for_child_wiki(building["latitude"], building["longitude"]),
                )
                created += 1

        if created:
            # One entry for the whole import: a hundred separate
            # "child_wiki_added" rows would bury every other edit in the
            # wiki's history.
            profile, _ = Profile.objects.get_or_create(user=request.user)
            WikiEdit.objects.create(
                wiki=wiki,
                editor=profile,
                changes={"child_wikis_imported": {"from": None, "to": f"{created} building markers"}},
            )
        return created

    @staticmethod
    def _toast(response: HttpResponse, level: str, message: str, *, refresh: bool = False) -> HttpResponse:
        """Attach a toast (and optionally a refresh event) to an HTMX response."""
        triggers: dict = {"showToast": {"level": level, "message": message}}
        if refresh:
            triggers["pinDetailPinsChanged"] = True
        response["HX-Trigger"] = json.dumps(triggers)
        return response
