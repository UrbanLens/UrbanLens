"""QuerySets and managers for wiki/pin articles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.abstract import DashboardManager, DashboardQuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class ArticleQuerySet(DashboardQuerySet):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.Article`."""

    def visible_to(self, profile: Profile) -> ArticleQuerySet:
        """Articles the given profile is allowed to read.

        Pin articles are strictly private to the pin's owner. Wiki articles
        follow the standard wiki visibility rule: the profile must have a pin
        at the wiki's location (or have created the wiki).

        Args:
            profile: The requesting profile.

        Returns:
            Queryset filtered to readable articles.
        """
        return self.filter(
            Q(pin__profile=profile) | Q(wiki__location__pins__profile=profile) | Q(wiki__created_by=profile),
        ).distinct()

    def with_content(self) -> ArticleQuerySet:
        """Articles that actually have article text (excludes empty stubs)."""
        return self.exclude(content="")


class ArticleManager(DashboardManager.from_queryset(ArticleQuerySet)):
    """Manager for Article."""


class ArticleRevisionQuerySet(DashboardQuerySet):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.ArticleRevision`."""


class ArticleRevisionManager(DashboardManager.from_queryset(ArticleRevisionQuerySet)):
    """Manager for ArticleRevision."""
