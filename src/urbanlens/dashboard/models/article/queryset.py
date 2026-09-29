"""QuerySets and managers for wiki/pin articles."""

from __future__ import annotations

from urbanlens.dashboard.models.abstract import DashboardManager, DashboardQuerySet


class ArticleQuerySet(DashboardQuerySet):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.Article`."""

    def with_content(self) -> ArticleQuerySet:
        """Articles that actually have article text (excludes empty stubs)."""
        return self.exclude(content="")


class ArticleManager(DashboardManager.from_queryset(ArticleQuerySet)):
    """Manager for Article."""


class ArticleRevisionQuerySet(DashboardQuerySet):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.ArticleRevision`."""


class ArticleRevisionManager(DashboardManager.from_queryset(ArticleRevisionQuerySet)):
    """Manager for ArticleRevision."""
