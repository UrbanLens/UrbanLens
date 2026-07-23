"""SearchHistory - recent global-search queries a user has run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, DateTimeField, ForeignKey, Index, PositiveIntegerField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.search_history.queryset import SearchHistoryManager

#: Longest query string persisted to history; longer inputs are truncated.
MAX_SEARCH_QUERY_LENGTH = 255


class SearchHistory(abstract.DashboardModel):
    """One remembered global-search query for a profile.

    A (profile, query) pair is stored once; re-running the same search bumps
    ``last_used`` and ``use_count`` instead of creating a duplicate row, so the
    recent-searches dropdown stays deduplicated and frecency-sortable.
    """

    query = CharField(max_length=MAX_SEARCH_QUERY_LENGTH)
    last_used = DateTimeField(auto_now=True, db_index=True)
    use_count = PositiveIntegerField(default=1)

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="search_history",
    )

    if TYPE_CHECKING:
        profile_id: int

    objects = SearchHistoryManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_search_history"
        ordering = ["-last_used"]
        verbose_name_plural = "search histories"
        constraints = [
            UniqueConstraint(fields=["profile", "query"], name="uniq_search_history_profile_query"),
        ]
        indexes = [Index(fields=["profile", "-last_used"], name="idx_search_history_recent")]

    def __str__(self) -> str:
        return f"SearchHistory({self.profile_id}: {self.query!r})"
