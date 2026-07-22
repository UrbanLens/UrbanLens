"""Manual sync between a pin's child pins and its wiki's child wikis.

Two endpoints, both pin-scoped and both no-ops (with a toast explaining why)
when the pin's location has no community wiki yet - see
``services.pin_wiki_sync`` for why neither ever creates one.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services import pin_wiki_sync

logger = logging.getLogger(__name__)


def _toast(response: HttpResponse, level: str, message: str, *, refresh: bool = False) -> HttpResponse:
    """Attach a toast (and optionally a refresh event) to an HTMX response."""
    triggers: dict = {"showToast": {"level": level, "message": message}}
    if refresh:
        triggers["pinDetailPinsChanged"] = True
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _no_wiki_or_up_to_date_message(pin: Pin, *, up_to_date_text: str) -> str:
    """Build the "nothing to do" toast for a sync call that created nothing."""
    if Wiki.objects.get_for_location(pin.location) is None:
        return "This property has no community wiki yet."
    return up_to_date_text


class PinSendToWikiView(LoginRequiredMixin, View):
    """POST: create a matching child wiki for each selected child pin.

    Body: repeated ``child_pin_uuids`` - the detail page's multi-select bulk
    toolbar's "Send to wiki" action.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)

        uuids = [u for u in request.POST.getlist("child_pin_uuids") if u]
        if not uuids:
            return HttpResponse("No sub pins selected.", status=400)
        children = list(pin.detail_pins.filter(uuid__in=uuids).select_related("location"))
        if not children:
            return _toast(HttpResponse("", status=200), "info", "Nothing to send - selection no longer matches any sub pin.")

        created = pin_wiki_sync.send_pins_to_wiki(pin, children, pin.profile)
        if created == 0:
            message = _no_wiki_or_up_to_date_message(pin, up_to_date_text="Every selected sub pin is already on the wiki.")
            return _toast(HttpResponse("", status=200), "info", message)
        return _toast(HttpResponse("", status=200), "success", f"Added {created} marker{'s' if created != 1 else ''} to the community wiki.")


class PinPullFromWikiView(LoginRequiredMixin, View):
    """POST: create a personal child pin for each of the wiki's child wikis not already covered."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)

        created = pin_wiki_sync.pull_children_from_wiki(pin)
        if created == 0:
            message = _no_wiki_or_up_to_date_message(pin, up_to_date_text="You already have a sub pin for everything on the wiki.")
            return _toast(HttpResponse("", status=200), "info", message)
        return _toast(HttpResponse("", status=200), "success", f"Added {created} sub pin{'s' if created != 1 else ''} from the community wiki.", refresh=True)
