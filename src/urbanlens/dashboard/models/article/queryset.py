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
        follow whatever ``services.wiki.wiki_access`` says, asked rather than
        restated: this used to check for a pin on the wiki's exact location or
        the ``created_by`` column, which is one of that rule's four clauses plus
        one it does not have. Both halves were visible to users - a pin sharing
        the place's domain opens the wiki page but not its article, and a
        creator with no pin could read an article on a page that answers 404.

        Args:
            profile: The requesting profile.

        Returns:
            Queryset filtered to readable articles.
        """
        from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids_cached

        return self.filter(
            Q(pin__profile=profile) | Q(wiki__location_id__in=visible_wiki_location_ids_cached(profile)),
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
