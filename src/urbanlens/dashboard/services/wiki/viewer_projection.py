"""
How a wiki reads to one viewer.

Every wiki-scoped surface gets its wiki through :func:`~urbanlens.dashboard.services.wiki.wiki_access.resolve_visible_wiki`,
which calls :func:`project_wiki`, and narrows each related queryset with :func:`visible_rows`. Both ask
:func:`viewer_projection`, so a rule about what a viewer is shown lives in one object rather than at each call site.

Core's projection is the identity: the stored rows, unchanged. A plugin replaces it through
:data:`~urbanlens.dashboard.plugins.points.WIKI_VIEWER_PROJECTION`, which fails closed.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from urbanlens.dashboard.plugins import points

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from django.db.models import QuerySet
    from django.db.models.base import ModelState

    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_stat_vote.queryset import WikiStatComposite


class ViewerProjection:
    """
    What one viewer is shown of one wiki.

    This base shows the stored rows unchanged. A plugin subclasses it, overrides what differs for the viewers it
    applies to, and returns it from a :data:`~urbanlens.dashboard.plugins.points.WIKI_VIEWER_PROJECTION` callback.
    Every method answers for the wiki and viewer the projection was built for.

    Attributes:
        wiki: The wiki, possibly a projection of one already.
        viewer: Who is looking, or None when signed out.
        active: Whether anything this viewer is shown differs from the stored rows.
    """

    active: bool = False

    def __init__(self, wiki: Wiki, viewer: Profile | None) -> None:
        self.wiki = wiki
        self.viewer = viewer

    @property
    def viewer_key(self) -> int | None:
        """The viewer's pk, or None when signed out."""
        return self.viewer.pk if self.viewer is not None else None

    def shown_wiki(self) -> Wiki:
        """The wiki as this viewer reads its fields: the row itself, or a copy that refuses to be written."""
        return self.wiki

    def rows[QuerySetT: QuerySet[Any]](self, queryset: QuerySetT) -> QuerySetT:
        """
        Narrow a queryset of rows scoped to this wiki to those this viewer is shown.

        Args:
            queryset: Rows already scoped to the wiki. A model's own queryset class is handed back.

        Returns:
            The narrowed queryset.
        """
        return queryset

    def newest_revision(self, article: Article) -> ArticleRevision | None:
        """The newest revision of ``article`` this viewer is shown, or None."""
        return self.rows(article.revisions.all()).order_by("-created", "-pk").first()

    def shown_article(self, article: Article | None) -> Article | None:
        """The article as this viewer reads it: the row itself, a copy carrying another revision's text, or None."""
        return article

    def community_summary(self, location: Location) -> dict[str, Any]:
        """The pinned-by summary for this wiki's place, in the shape ``wiki_community_summary`` returns."""
        from urbanlens.dashboard.services.wiki.community_counts import wiki_community_summary

        return wiki_community_summary(self.wiki, location)

    def stat_composite(self, field: str) -> WikiStatComposite:
        """The community composite of one stat field, as ``WikiStatVoteQuerySet.composite`` returns it."""
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

        return WikiStatVote.objects.composite(self.wiki, field)

    def public_vote(self, location: Location) -> dict[str, Any] | None:
        """The template context of the public-pin vote block, or None to render nothing."""
        from urbanlens.dashboard.services.pins.public_pins import public_vote_context

        return public_vote_context(location, self.viewer)

    def boundary_vote(self, place: Place | None) -> dict[str, Any] | None:
        """The template context of the boundary-vote dialog, or None to render nothing."""
        from urbanlens.dashboard.services.geo.boundary_voting import boundary_vote_context

        return boundary_vote_context(place, self.viewer)

    def edit_changes(self, changes: Any) -> Any:
        """An edit's stored diff as this viewer is shown it."""
        return changes

    def shows_cover_photo(self) -> bool:
        """Whether the wiki's cover photo is shown."""
        return True

    def shows_wiki_outline(self) -> bool:
        """Whether an outline drawn on the wiki itself is shown, rather than the place's."""
        return True

    def shows_build_year(self) -> bool:
        """Whether the wiki's build year is shown."""
        return True

    def shows_wiki_link(self) -> bool:
        """Whether a page that merely belongs to the wiki (a photo's lightbox) links to it."""
        return True

    def shows_change(self, actor: Profile, fields: Collection[str]) -> bool:
        """
        Whether this viewer is told of a change to the wiki.

        Args:
            actor: Who made the change.
            fields: The wiki fields it wrote, empty when it wrote none of the wiki's own.
        """
        return True

    def shows_comment(self, comment: Comment) -> bool:
        """Whether this viewer is shown ``comment``, one of the wiki's."""
        return True

    def matches_wiki_text(self, terms: list[str]) -> bool:
        """
        Whether every search term appears in the wiki's text as this viewer reads it.

        A database match runs against the stored row, so a projection that rewrites text re-checks here.
        """
        return True

    def matches_article_text(self, article: Article, terms: list[str]) -> bool:
        """Whether every search term appears in ``article`` and its wiki's text as this viewer reads them."""
        return True

    @classmethod
    def prefetch(cls, projections: Iterable[ViewerProjection]) -> None:
        """Load, in one pass, whatever a batch of this class's projections will read."""


class Projectable(Protocol):
    """A model that can exist as a read-only projection of itself: ``Wiki`` and ``Article``."""

    _ul_projection: ViewerProjection | None
    _state: ModelState


def viewer_projection(wiki: Wiki, viewer: Profile | None) -> ViewerProjection:
    """
    How ``viewer`` reads ``wiki``.

    Args:
        wiki: The wiki, possibly a projection of one.
        viewer: Who is looking, or None when signed out.

    Returns:
        The identity projection unless a plugin replaces it.
    """
    return points.WIKI_VIEWER_PROJECTION.apply(ViewerProjection(wiki, viewer), wiki, viewer)


def prefetch_projections(projections: Iterable[ViewerProjection]) -> None:
    """Let each kind of projection load what the batch will read at once rather than per wiki."""
    batches: dict[type[ViewerProjection], list[ViewerProjection]] = {}
    for projection in projections:
        batches.setdefault(type(projection), []).append(projection)
    for kind, batch in batches.items():
        kind.prefetch(batch)


def project_wiki(wiki: Wiki, viewer: Profile | None) -> Wiki:
    """
    Return ``wiki`` as ``viewer`` reads it.

    Args:
        wiki: The real row, or a projection built for some viewer.
        viewer: Who is looking, or None when signed out.

    Returns:
        The wiki itself or a copy that must not be written. A projection already built for this viewer comes back
        as it is; one built for another is rebuilt from the stored row.
    """
    existing = projection_of(wiki)
    if existing is not None:
        if existing.viewer_key == (viewer.pk if viewer is not None else None):
            return wiki
        wiki = _real_row(wiki)
    return viewer_projection(wiki, viewer).shown_wiki()


def project_article(article: Article | None, wiki: Wiki, viewer: Profile | None) -> Article | None:
    """``article``, the article hosted on ``wiki``, as ``viewer`` reads it; None when they read none."""
    return viewer_projection(wiki, viewer).shown_article(article)


def visible_rows[QuerySetT: QuerySet[Any]](queryset: QuerySetT, wiki: Wiki, viewer: Profile | None) -> QuerySetT:
    """
    A wiki-scoped queryset narrowed to what ``viewer`` is shown.

    Args:
        queryset: Rows already scoped to ``wiki``.
        wiki: The wiki they belong to.
        viewer: Who is looking, or None when signed out.

    Returns:
        The queryset.
    """
    return viewer_projection(wiki, viewer).rows(queryset)


def projection_of(row: Projectable) -> ViewerProjection | None:
    """The projection ``row`` is a copy under, or None when it is a real row."""
    return row._ul_projection  # noqa: SLF001


def is_projected(row: Projectable) -> bool:
    """Whether this object is a projection rather than a real row."""
    return projection_of(row) is not None


def projected_copy[RowT: Projectable](row: RowT, projection: ViewerProjection) -> RowT:
    """
    A copy of ``row`` that refuses to be saved or deleted, for a projection to overwrite with what its viewer is shown.

    A projection carries values the stored row does not, so persisting it would write them over the real row.

    Args:
        row: The real row.
        projection: The projection the copy is made under.

    Returns:
        The copy, marked as a projection of ``projection``.
    """
    clone = copy.copy(row)
    # The shallow copy shares _state; copy it so the real row's cached relations are not touched.
    clone._state = copy.copy(row._state)  # noqa: SLF001
    clone._state.fields_cache = dict(row._state.fields_cache)  # noqa: SLF001
    clone._ul_projection = projection  # noqa: SLF001
    # Instance entries shadow the methods they replace.
    clone.__dict__["save"] = _refuse_write
    clone.__dict__["delete"] = _refuse_write
    return clone


def _refuse_write(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise TypeError("this row is a viewer's projection and must not be written; re-fetch the row")


def _real_row(wiki: Wiki) -> Wiki:
    """Re-read ``wiki`` from the database, discarding any projection applied to it."""
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

    return WikiModel.objects.select_related("location", "place").get(pk=wiki.pk)


def writable_wiki(wiki: Wiki) -> Wiki:
    """
    Return a row that may be written: ``wiki`` itself, or a fresh fetch of it.

    Args:
        wiki: A wiki, possibly a projection.

    Returns:
        A wiki safe to mutate and save.
    """
    if not is_projected(wiki):
        return wiki
    return _real_row(wiki)
