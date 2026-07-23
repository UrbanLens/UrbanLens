"""Global search controllers: the dialog panel, history recording, and deletion."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.search_history import SearchHistory
from urbanlens.dashboard.services.global_search import GlobalSearchEngine

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Candidate example queries for the empty dialog's "Try searching for" section, in
#: priority order - richer natural-language examples first (they show off more of
#: what search understands), broad single-type fallbacks last (near-guaranteed to
#: have results for any user with that kind of data at all). Never shown as-is -
#: see _verified_hints, which only surfaces ones that actually return a result for
#: the requesting profile, per the resolved backlog item requiring suggestions that
#: won't dead-end the user.
SEARCH_HINT_CANDIDATES = (
    "pins near me",
    "photos from this year",
    "trips this year",
    "pins",
    "photos",
    "wikis",
    "messages",
    "maps",
    "trips",
)
#: How many verified hints to show at once - matches the dialog's original fixed layout.
MAX_SEARCH_HINTS = 4
#: Short-lived: just avoids re-running several searches on every dialog open:
#: new pins/photos/etc show up in hints within this window either way.
SEARCH_HINTS_CACHE_SECONDS = 60 * 15


def _verified_hints(profile: Profile) -> list[str]:
    """Return up to MAX_SEARCH_HINTS candidate queries that actually return a result for this profile.

    Args:
        profile: The requesting user's profile.

    Returns:
        Candidates from SEARCH_HINT_CANDIDATES, in priority order, that each
        returned at least one search result - never a query guaranteed to
        dead-end the user in an empty state.
    """
    cache_key = f"search_hints:{profile.pk}"
    cached: list[str] | None = cache.get(cache_key)
    if cached is not None:
        return cached

    engine = GlobalSearchEngine()
    verified: list[str] = []
    for candidate in SEARCH_HINT_CANDIDATES:
        if len(verified) >= MAX_SEARCH_HINTS:
            break
        try:
            if engine.search(profile, candidate).total > 0:
                verified.append(candidate)
        except Exception:
            logger.exception("Failed to verify search hint %r", candidate)

    cache.set(cache_key, verified, SEARCH_HINTS_CACHE_SECONDS)
    return verified


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


class GlobalSearchHintsView(LoginRequiredMixin, View):
    """Verified "Try searching for" example queries, loaded separately from the
    main panel so running several candidate searches never delays the dialog
    opening - see _panel.html's hx-get on this view.

    GET /search/hints/ → the hint-buttons fragment.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render only the example queries confirmed to return a result.

        Args:
            request: GET, no params.

        Returns:
            The ``_search_hints.html`` fragment.
        """
        profile = _get_profile(request)
        return render(
            request,
            "dashboard/partials/search/_search_hints.html",
            {"search_hints": _verified_hints(profile)},
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
            },
        )
