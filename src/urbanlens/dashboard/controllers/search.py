"""Global search controllers: the dialog panel, history recording, and deletion."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.search_history import SearchHistory
from urbanlens.dashboard.services.global_search import GlobalSearchEngine

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Example queries shown in the empty dialog to teach the natural-language syntax.
SEARCH_HINTS = (
    "photos from last summer",
    "pins in Cincinnati",
    "trips this year",
    "messages about meetup",
)


def _get_profile(request: HttpRequest) -> Profile:
    """Resolve (or lazily create) the requesting user's profile."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


class GlobalSearchPanelView(LoginRequiredMixin, View):
    """The search dialog's swap target: suggestions when empty, results otherwise."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render suggestions (blank query) or grouped search results.

        Args:
            request: GET with ``q`` (the query, may be blank).

        Returns:
            The ``_panel.html`` partial for the dialog body.
        """
        profile = _get_profile(request)
        query = (request.GET.get("q") or "").strip()

        if not query:
            return render(
                request,
                "dashboard/partials/search/_panel.html",
                {
                    "query": "",
                    "recent_searches": SearchHistory.objects.recent_for(profile),
                    "search_hints": SEARCH_HINTS,
                },
            )

        response = GlobalSearchEngine().search(profile, query)
        return render(
            request,
            "dashboard/partials/search/_panel.html",
            {
                "query": query,
                "response": response,
                "filter_chips": response.parsed.describe_filters(),
            },
        )


class GlobalSearchCommitView(LoginRequiredMixin, View):
    """Records a query into search history.

    Called by the dialog when the user commits to a search - pressing Enter or
    clicking a result - rather than on every debounced keystroke, so history
    holds intentional searches instead of prefixes.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Remember ``q`` for the requesting user.

        Args:
            request: POST with ``q``.

        Returns:
            204 always; history is best-effort.
        """
        query = (request.POST.get("q") or "").strip()
        if query:
            try:
                SearchHistory.objects.record(_get_profile(request), query)
            except Exception:
                logger.exception("Failed to record search history")
        return HttpResponse(status=204)


class GlobalSearchHistoryDeleteView(LoginRequiredMixin, View):
    """Deletes one remembered search (or all of them) and re-renders the panel."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Delete history rows for the requesting user.

        Args:
            request: POST with either ``history_id`` (one row) or ``all=1``.

        Returns:
            The refreshed empty-query panel partial.
        """
        profile = _get_profile(request)
        history_id = request.POST.get("history_id")
        if request.POST.get("all"):
            SearchHistory.objects.for_profile(profile).delete()
        elif history_id and history_id.isdigit():
            SearchHistory.objects.filter(profile=profile, pk=int(history_id)).delete()
        return render(
            request,
            "dashboard/partials/search/_panel.html",
            {
                "query": "",
                "recent_searches": SearchHistory.objects.recent_for(profile),
                "search_hints": SEARCH_HINTS,
            },
        )
