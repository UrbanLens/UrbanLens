"""Memories → Locations page: review queue for batch-scan pin suggestions.

Suggestions are produced in bulk by ``services.pin_suggestions.ingest_location_hits``
(called from the Immich full-library sweep and the Tools-page local folder scanner) -
this controller only lets the owner accept or reject what was already found; it never
triggers a scan itself.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins
from urbanlens.dashboard.services.pin_suggestions import accept_pin_suggestion, reject_pin_suggestion

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_QUEUE_PARTIAL = "dashboard/partials/memories/_pin_suggestions_queue.html"
_CARD_PARTIAL = "dashboard/partials/memories/_pin_suggestion_card.html"


def _pending_suggestions(profile: Profile) -> list[PinSuggestion]:
    """Return the profile's pending suggestions, newest first."""
    return list(PinSuggestion.objects.for_profile(profile).pending().select_related("pin", "pin__location").order_by("-created"))


def _toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Mirrors ``controllers.photos._toast``.
    """
    triggers: dict[str, Any] = {"showToast": {"message": message, "level": level}}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response


class PinSuggestionQueueView(LoginRequiredMixin, View):
    """The Locations subpage of Memories - the batch-scan suggestion review queue.

    GET /memories/locations/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestions = _pending_suggestions(profile)
        return render(
            request,
            "dashboard/pages/memories/locations.html",
            {
                "page_name": "memories",
                "suggestions": suggestions,
                "unlogged_visits_count": len(unlogged_visited_pins(profile)),
                "pin_suggestions_count": len(suggestions),
            },
        )


class PinSuggestionQueuePartialView(LoginRequiredMixin, View):
    """Just the suggestion queue partial, re-fetched via the ``refreshQueue`` event.

    GET /memories/locations/queue/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(request, _QUEUE_PARTIAL, {"suggestions": _pending_suggestions(profile)})


class PinSuggestionActionView(LoginRequiredMixin, View):
    """Accept or reject a single pin suggestion.

    POST /memories/locations/<suggestion_id>/<action>/ where action is "accept" or "reject".
    """

    def _get_suggestion(self, request: HttpRequest, suggestion_id: int) -> tuple[PinSuggestion, Profile]:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion = get_object_or_404(PinSuggestion.objects.select_related("pin", "pin__location"), pk=suggestion_id)
        if suggestion.profile_id != profile.pk:
            raise Http404
        return suggestion, profile

    def post(self, request: HttpRequest, suggestion_id: int, action: str) -> HttpResponse:
        if action not in {"accept", "reject"}:
            raise Http404
        suggestion, profile = self._get_suggestion(request, suggestion_id)
        if not suggestion.is_actionable:
            return _toast("That suggestion has already been handled.", "info", refresh_queue=True)

        try:
            if action == "reject":
                reject_pin_suggestion(suggestion)
                return _toast("Suggestion dismissed.", "info", refresh_queue=True)

            pin, visits = accept_pin_suggestion(suggestion, profile)
            if not visits:
                message = f"{'Pin created' if suggestion.is_new_pin else 'Saved'}. Visit logging is turned off, so no visit was recorded."
                return _toast(message, "info", refresh_queue=True)
            plural = "s" if len(visits) != 1 else ""
            verb = "Pin created and" if suggestion.is_new_pin else ""
            message = f"{verb} {len(visits)} visit{plural} logged for {pin.effective_name}.".strip()
            return _toast(message, refresh_queue=True)
        except Exception:
            logger.exception("Pin suggestion action '%s' failed for suggestion %s", action, suggestion_id)
            response = render(request, _CARD_PARTIAL, {"suggestion": suggestion})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": "Something went wrong. Please try again.", "level": "error"}})
            return response
