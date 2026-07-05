"""Controller for responding to a suggested visit entry (accept/reject from the notification dropdown)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.notifications.meta import Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.services.visits import accept_visit_suggestion, merge_visit_suggestion, reject_visit_suggestion

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
        """Handle an accept/reject response and return the refreshed notification dropdown.

        Args:
            request: Incoming HTTP request.
            suggestion_id: Primary key of the VisitSuggestion being responded to.

        Returns:
            Rendered notification dropdown partial, with the badge-refresh trigger set.
        """
        suggestion = get_object_or_404(VisitSuggestion, pk=suggestion_id, suggested_to=request.user.profile)
        action = request.POST.get("action")
        if suggestion.status == VisitSuggestionStatus.PENDING:
            if suggestion.offers_merge:
                if action == "add_participants":
                    merge_visit_suggestion(suggestion, request.user.profile)
                elif action == "new_entry":
                    accept_visit_suggestion(suggestion, request.user.profile)
                elif action == "reject":
                    reject_visit_suggestion(suggestion)
            elif action == "accept":
                accept_visit_suggestion(suggestion, request.user.profile)
            elif action == "reject":
                reject_visit_suggestion(suggestion)

        if suggestion.notification_id:
            NotificationLog.objects.filter(pk=suggestion.notification_id).update(status=Status.READ)

        from urbanlens.dashboard.controllers.notifications import _trigger_badge_refresh

        notifications = NotificationLog.objects.for_profile(request.user.profile).select_related("source_profile").order_by("-created")[:20]
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {"notifications": notifications, "unread_count": NotificationLog.objects.for_profile(request.user.profile).unread().count()},
        )
        return _trigger_badge_refresh(response)
