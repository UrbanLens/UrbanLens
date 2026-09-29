"""QuerySets and managers for wiki/pin articles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract import DashboardManager, DashboardQuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision  # noqa: F401 - mypy needs these; ruff does not


class ArticleQuerySet(DashboardQuerySet["Article"]):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.Article`."""

    def with_content(self) -> ArticleQuerySet:
        """Articles that actually have article text (excludes empty stubs)."""
        return self.exclude(content="")


_ArticleManagerBase = DashboardManager.from_queryset(ArticleQuerySet)


class ArticleManager(_ArticleManagerBase):
    """Manager for Article."""


class ArticleRevisionQuerySet(DashboardQuerySet["ArticleRevision"]):
    """Custom queryset for :class:`~urbanlens.dashboard.models.article.model.ArticleRevision`."""


_ArticleRevisionManagerBase = DashboardManager.from_queryset(ArticleRevisionQuerySet)


class ArticleRevisionManager(_ArticleRevisionManagerBase):
    """Manager for ArticleRevision."""
