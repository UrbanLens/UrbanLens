"""Controller for responding to a suggested visit entry (accept/reject from the notification dropdown)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.controllers.notifications import action_taken_response
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.services.visits.visits import accept_visit_suggestion, merge_visit_suggestion, reject_visit_suggestion

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class VisitSuggestionRespondView(LoginRequiredMixin, View):
    """Accept or reject a suggested visit entry from the notification dropdown.

    POST /visit-suggestions/<int:suggestion_id>/respond/
    Body: action=accept|reject for a first-time suggestion, or
          action=add_participants|new_entry|reject when suggestion.offers_merge
          (suggested_to already has a visit logged for this place and date).
    """

    def post(self, request: HttpRequest, suggestion_id: int) -> HttpResponse:
        """Handle an accept/reject response and return an HTMX swap payload.

        Args:
            request: Incoming HTTP request.
            suggestion_id: Primary key of the VisitSuggestion being responded to.

        Returns:
            Empty body (inbox row removal), re-rendered history row, or pin
            visit-history partial - depending on ``surface`` / ``context``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion = get_object_or_404(
            VisitSuggestion.objects.select_related("notification"),
            pk=suggestion_id,
            suggested_to=profile,
        )
        action = request.POST.get("action")
        blocked = False
        if suggestion.status == VisitSuggestionStatus.PENDING:
            if suggestion.offers_merge:
                if action == "add_participants":
                    merge_visit_suggestion(suggestion, profile)
                elif action == "new_entry":
                    blocked = accept_visit_suggestion(suggestion, profile) is None
                elif action == "reject":
                    reject_visit_suggestion(suggestion)
            elif action == "accept":
                blocked = accept_visit_suggestion(suggestion, profile) is None
            elif action == "reject":
                reject_visit_suggestion(suggestion)

        # Built once and applied to whichever response is returned: the pin-context
        # branch below used to return before the `blocked` toast was attached, so a
        # response the user had explicitly asked for silently did nothing when visit
        # logging was off.
        extra_triggers: dict = {}
        if blocked:
            extra_triggers["showToast"] = {
                "message": "Visit logging is turned off - enable it in Settings to add this to your visit history.",
                "level": "info",
            }

        if request.POST.get("context") == "pin" and request.POST.get("pin_slug"):
            from urbanlens.dashboard.controllers.visits import _render_visit_history
            from urbanlens.dashboard.models.pin.model import Pin

            pin = get_object_or_404(Pin, slug=request.POST["pin_slug"], profile__user=request.user)
            response = _render_visit_history(request, pin)
            triggers: dict = {"notifCountRefresh": {"target": "body"}}
            triggers.update(extra_triggers)
            response["HX-Trigger"] = json.dumps(triggers)
            return response

        return action_taken_response(
            request,
            profile,
            notification=suggestion.notification,
            extra_triggers=extra_triggers or None,
        )
