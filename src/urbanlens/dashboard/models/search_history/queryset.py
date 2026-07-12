"""QuerySet and Manager for SearchHistory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import F

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.search_history.model import SearchHistory

#: Rows kept per profile; older entries are pruned on each record().
MAX_HISTORY_PER_PROFILE = 50


class SearchHistoryQuerySet(abstract.DashboardQuerySet["SearchHistory"]):
    """QuerySet for remembered global-search queries."""

    def for_profile(self, profile: Profile) -> Self:
        """All history rows belonging to a profile, most recent first.

        Args:
            profile: The profile whose search history to return.

        Returns:
            Filtered queryset ordered by ``last_used`` descending.
        """
        return self.filter(profile=profile).order_by("-last_used")

    def recent_for(self, profile: Profile, limit: int = 8) -> list[SearchHistory]:
        """The profile's most recently used queries.

        Args:
            profile: The profile whose history to return.
            limit: Maximum number of rows.

        Returns:
            Up to ``limit`` history rows, most recent first.
        """
        return list(self.for_profile(profile)[:limit])


class SearchHistoryManager(abstract.DashboardManager.from_queryset(SearchHistoryQuerySet)):
    """Manager for SearchHistory with the record/prune helper."""

    def record(self, profile: Profile, query: str) -> SearchHistory | None:
        """Remember a search query for a profile, deduplicating repeats.

        Re-running an existing query bumps ``last_used``/``use_count`` instead
        of inserting a duplicate. History beyond ``MAX_HISTORY_PER_PROFILE``
        rows is pruned oldest-first.

        Args:
            profile: The profile that ran the search.
            query: The raw query string; blank/whitespace-only is ignored.

        Returns:
            The created or refreshed row, or None when the query was blank.
        """
        from urbanlens.dashboard.models.search_history.model import MAX_SEARCH_QUERY_LENGTH

        cleaned = " ".join(query.split())[:MAX_SEARCH_QUERY_LENGTH]
        if not cleaned:
            return None

        row, created = self.get_or_create(profile=profile, query=cleaned)
        if not created:
            # auto_now refreshes last_used on save; use_count via F to stay race-safe.
            row.use_count = F("use_count") + 1
            row.save(update_fields=["use_count", "last_used", "updated"])
            row.refresh_from_db(fields=["use_count"])

        stale_ids = self.for_profile(profile).values_list("pk", flat=True)[MAX_HISTORY_PER_PROFILE:]
        if stale_ids:
            self.filter(pk__in=list(stale_ids)).delete()
        return row
