"""A stand-in viewer projection, so core's seam is tested without any plugin installed."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.services.wiki.viewer_projection import ViewerProjection, projected_copy

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: The column that says who contributed a row, whichever of these the model has.
_ACTOR_COLUMNS = ("profile_id", "editor_id", "created_by_id")

PROJECTED_NAME = "Name as projected"


class HidingProjection(ViewerProjection):
    """
    A projection that hides what some profiles contributed and the wiki-level extras around it.

    Shows nothing but the stored rows of everyone else, a fixed name, and no cover photo, outline, build year or
    wiki link.
    """

    active = True

    def __init__(self, wiki: Wiki, viewer: Profile | None, *, hidden_profile_ids: Collection[int] = ()) -> None:
        super().__init__(wiki, viewer)
        self.hidden = frozenset(hidden_profile_ids)

    def rows[QuerySetT: QuerySet[Any]](self, queryset: QuerySetT) -> QuerySetT:
        column = next(
            name for name in _ACTOR_COLUMNS if name in {field.attname for field in queryset.model._meta.concrete_fields}
        )
        return queryset.exclude(**{f"{column}__in": self.hidden})

    def shown_wiki(self) -> Wiki:
        projection = projected_copy(self.wiki, self)
        projection.name = PROJECTED_NAME
        return projection

    def shown_article(self, article: Article | None) -> Article | None:
        if article is None:
            return None
        revision = self.newest_revision(article)
        if revision is None:
            return None
        projection = projected_copy(article, self)
        projection.content = revision.content
        return projection

    def shows_cover_photo(self) -> bool:
        return False

    def shows_wiki_outline(self) -> bool:
        return False

    def shows_build_year(self) -> bool:
        return False

    def shows_wiki_link(self) -> bool:
        return False

    def shows_change(self, actor: Profile, fields: Collection[str]) -> bool:
        return actor.pk not in self.hidden and "cameras" not in fields

    def shows_comment(self, comment: Comment) -> bool:
        return comment.profile_id not in self.hidden

    def matches_wiki_text(self, terms: list[str]) -> bool:
        return all(term in PROJECTED_NAME.lower() for term in terms)


@contextmanager
def projecting(
    applies: Callable[[Wiki, Profile | None], bool] = lambda wiki, viewer: True,
    *,
    hidden_profile_ids: Collection[int] = (),
) -> Iterator[None]:
    """
    Make wikis read through :class:`HidingProjection` wherever ``applies`` says so, and nothing else contribute.

    Args:
        applies: Which wiki and viewer the projection is for; the stored rows are read for the rest.
        hidden_profile_ids: Whose contributions the projection hides.
    """

    def project(current: ViewerProjection, wiki: Wiki, viewer: Profile | None) -> ViewerProjection:
        return (
            HidingProjection(wiki, viewer, hidden_profile_ids=hidden_profile_ids) if applies(wiki, viewer) else current
        )

    with points.WIKI_VIEWER_PROJECTION.overridden([project]):
        yield


@contextmanager
def failing_projection(error: Exception) -> Iterator[None]:
    """Make the projection seam hold one callback that raises ``error``."""

    def fail(current: ViewerProjection, wiki: Wiki, viewer: Profile | None) -> ViewerProjection:
        raise error

    with points.WIKI_VIEWER_PROJECTION.overridden([fail]):
        yield
