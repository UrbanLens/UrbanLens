"""
Deciding what a wiki shows to a viewer who has not earned its detail yet.
A wiki row exists for every place, so absence is itself a tell - and so is any visible difference in how the site behaves for this account."""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from django.db.models import Q

from urbanlens.dashboard.models.abstract.versioned import concrete_field, resolve_fields
from urbanlens.dashboard.models.abstract.versioning import WriteSource

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Fields forced to their unset value regardless of who wrote them.
#: Security indicators are the one category the product owner ruled on directly: always unset,
#: whether a person or a provider supplied them, because the whole point of concealment is that a
#: place must not read as one people have surveyed.
ALWAYS_UNSET: tuple[str, ...] = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

#: Cached per request on the Profile instance, like
#: ``visible_wiki_locations_cached`` - a Profile is loaded fresh per request,
#: so the entry cannot outlive one, and nothing has to invalidate it.
_FRIEND_CACHE_ATTR = "_ul_accepted_friend_ids"


def accepted_friend_ids(profile: Profile) -> set[int]:
    """
    Return the profile pks this profile has an accepted friendship with.

    Args:
        profile: The viewer.

    Returns:
        Profile pks, not including the viewer's own."""
    cached = getattr(profile, _FRIEND_CACHE_ATTR, None)
    if cached is not None:
        return cached

    from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

    rows = Friendship.objects.filter(
        Q(from_profile=profile) | Q(to_profile=profile),
        status=FriendshipStatus.ACCEPTED,
    ).values_list("from_profile_id", "to_profile_id")

    ids = {pk for row in rows for pk in row if pk != profile.pk}
    setattr(profile, _FRIEND_CACHE_ATTR, ids)
    return ids


def visible_actor_ids(profile: Profile | None) -> set[int]:
    """
    Return whose contributions a concealed viewer may still see.

    Args:
        profile: The viewer, or None for a signed-out caller.

    Returns:
        The viewer's own pk plus their accepted friends'.
    """
    if profile is None:
        return set()
    return {profile.pk} | accepted_friend_ids(profile)


def concealed_field_values(wiki: Wiki, viewer: Profile | None) -> dict[str, Any]:
    """
    Return the field values a concealed viewer should be shown.

    Args:
        wiki: The wiki being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        ``{field_name: value}`` covering every versioned field."""
    resolved = resolve_fields(
        wiki,
        sources=(WriteSource.AUTOMATIC,),
        actor_ids=visible_actor_ids(viewer),
    )

    values: dict[str, Any] = {}
    for name in wiki.versioned_fields:
        field = concrete_field(type(wiki), name)
        if field is None:
            continue
        if name not in ALWAYS_UNSET and name in resolved:
            values[name] = resolved[name]
            continue
        # Either the field is unset by rule, or nobody this viewer can see has
        # written it. Either way fall back to the field's default rather than
        # the live value - the live value is precisely what is being concealed.
        values[name] = field.get_default()

    # `name` has no model default, so the fallback above yields "".
    # No wiki ever looks like that: `get_or_create_for_location`, the one creation path, always names it.
    if not values.get("name"):
        from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

        location = wiki.location
        values["name"] = (location.provider_name if location else "") or WikiModel.objects._placeholder_name(location)  # noqa: SLF001
    return values


def concealment_active(wiki: Wiki, viewer: Profile | None) -> bool:
    """
    Whether this viewer should be shown the concealed form of this wiki.

    Args:
        wiki: The wiki being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        Whether to conceal."""
    return False


def concealed_community_summary() -> dict[str, Any]:
    """
    The community card as it reads for a place nobody has pinned but you.
    That fuzz caches its value for a day keyed **only on the id passed in**, with no viewer in the key.

    Returns:
        The same shape ``wiki_community_summary`` returns, in its empty state."""
    return {
        "pin_count_low": True,
        "pin_count_approx": None,
        "first_pinned": None,
        "first_pinned_precision": "month",
    }


#: How to tell, per related model, whether a row was contributed by a person and if so by whom.
#: Recorded here as a table rather than as a rule each queryset re-states, because the failure mode
#: this whole feature exists to avoid is a rule spelled out per call site and forgotten at one of
#: them.
_NULL_ACTOR_IS_A_DELETED_ACCOUNT: frozenset[str] = frozenset({"WikiEdit"})

_ACTOR_FIELDS: dict[str, str] = {
    # A child Wiki is a detail pin. Null means mirrored from building data
    # (services.pins.pin_restructure), which a brand-new wiki would carry, so
    # the generic null-is-automatic rule is right here.
    "Wiki": "created_by_id",
    # Markup items record who drew them. A concealed viewer keeps their own
    # drawings and their friends' - see the note in controllers/markup.py about
    # why that does not reopen what hiding markup was for.
    "PinMarkup": "profile_id",
    "Comment": "profile_id",
    "WikiEdit": "editor_id",
    "WikiLink": "created_by_id",
    "WikiStatVote": "profile_id",
    "Floorplan": "profile_id",
    "MarkupMap": "profile_id",
    # Layers/overlays and reactions carry the same "own contribution is not what concealment exists
    # to hide" reasoning as markup - see controllers/custom_layers.py and
    # controllers/map_overlays.py.
    # `profile` is non-null CASCADE on all three, so the isnull half of conceal_rows' generic clause
    "CustomLayer": "profile_id",
    "MapImageOverlay": "profile_id",
    "Reaction": "profile_id",
    "Album": "profile_id",
    # Ownership/sale records.
    # `created_by` is SET_NULL on account deletion - but unlike WikiEdit,
    # `plugins.builtin.property_records` also writes real OFFICIAL-sourced rows
    # (deed/mailing-address lookups) with no `created_by` at all, so a null actor here is genuinely
    "WikiOwner": "created_by_id",
    "WikiPropertySale": "created_by_id",
}


def conceal_rows[QuerySetT: QuerySet[Any]](queryset: QuerySetT, viewer: Profile | None) -> QuerySetT:
    """
    Narrow a wiki-scoped queryset to what a concealed viewer may see.

    Args:
        queryset: Rows already scoped to one wiki. Generic, so a caller that
            handed in a model's own queryset class gets it back rather than a
            bare ``QuerySet`` - narrowing rows never changes what a queryset
            can do.
        viewer: Who is looking, or None when signed out.

    Returns:
        The narrowed queryset."""
    from urbanlens.dashboard.models.images.model import Image

    model_name = queryset.model.__name__
    allowed = visible_actor_ids(viewer)

    if queryset.model is Image:
        # Provider rows stay (fresh wiki content). User photos stay only for viewer or friends.
        from urbanlens.dashboard.models.images.queryset import _own_contribution_q

        return queryset.filter(~_own_contribution_q() | Q(profile_id__in=allowed))

    if model_name == "WikiAlias":
        # created_by is NULL for geocoder backfill and auto-rename; filter by source instead.
        from urbanlens.dashboard.models.aliases.model import AliasSource

        return queryset.filter(~Q(source=AliasSource.USER) | Q(created_by_id__in=allowed))

    if model_name == "ArticleRevision":
        # Null editor = Wikipedia seed (allowed) or deleted account (blocked). Distinguish by edit summary.
        from urbanlens.dashboard.models.article.model import SYSTEM_EDIT_SUMMARIES

        return queryset.filter(Q(editor_id__in=allowed) | Q(editor_id__isnull=True, edit_summary__in=SYSTEM_EDIT_SUMMARIES))

    actor_field = _ACTOR_FIELDS.get(model_name)
    if actor_field is None:
        logger.error("conceal_rows: no provenance rule for %s; refusing to guess", model_name)
        return queryset.none()

    if model_name in _NULL_ACTOR_IS_A_DELETED_ACCOUNT:
        return queryset.filter(**{f"{actor_field}__in": allowed})

    return queryset.filter(Q(**{f"{actor_field}__isnull": True}) | Q(**{f"{actor_field}__in": allowed}))


def visible_rows(queryset: Any, wiki: Wiki, viewer: Profile | None) -> Any:
    """
    A wiki-scoped queryset narrowed to what *viewer* may see.

    Args:
        queryset: Rows already scoped to one wiki.
        wiki: The wiki they belong to, for the concealment decision.
        viewer: Who is looking, or None when signed out.

    Returns:
        The queryset, narrowed when this viewer is concealed."""
    return conceal_rows(queryset, viewer) if concealment_active(wiki, viewer) else queryset


def _real_row(wiki: Wiki) -> Wiki:
    """Re-read *wiki* from the database, discarding any concealment applied to it."""
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

    return WikiModel.objects.select_related("location", "place").get(pk=wiki.pk)


def _refuse_write(*_args: Any, **_kwargs: Any) -> NoReturn:
    """
    Refuse a write through a concealed projection.
    A projection carries concealed values for fields this viewer may not see, so persisting it would write those values over the real row - a concealment bug that silently destroys community content."""
    raise TypeError("this Wiki is a concealed projection and must not be written; re-fetch the row")


def conceal_wiki(wiki: Wiki, viewer: Profile | None) -> Wiki:
    """
    Return *wiki* itself, or a concealed projection of it.

    Args:
        wiki: The real row.
        viewer: Who is looking, or None when signed out.

    Returns:
        The real wiki, or a write-refusing projection of it."""
    viewer_key = viewer.pk if viewer else None
    if is_concealed(wiki):
        # Idempotent: multiple surfaces call this on already-concealed wikis.
        if wiki._ul_concealed_for == viewer_key:  # noqa: SLF001
            return wiki
        # Projection built for another viewer; must re-fetch the real row.
        wiki = _real_row(wiki)

    if not concealment_active(wiki, viewer):
        return wiki

    projection = copy.copy(wiki)
    # Shallow copy shares _state; copy it to avoid mutating the real row's cached relations.
    projection._state = copy.copy(wiki._state)  # noqa: SLF001
    projection._state.fields_cache = dict(wiki._state.fields_cache)  # noqa: SLF001
    for name, value in concealed_field_values(wiki, viewer).items():
        setattr(projection, name, value)

    # Mark for assertions and tests to distinguish projection from real row.
    projection._ul_concealed = True  # noqa: SLF001
    projection._ul_concealed_for = viewer_key  # noqa: SLF001
    projection.save = _refuse_write  # type: ignore[method-assign]
    projection.delete = _refuse_write  # type: ignore[method-assign]
    return projection


class Concealable(Protocol):
    """
    A model that can exist as a concealed projection of itself.
    Two do - ``Wiki`` and ``Article`` - and both declare the marker on the model rather than having it set on them ad hoc, so the distinction is visible where the row is defined."""

    _ul_concealed: bool


def is_concealed(row: Concealable) -> bool:
    """Whether this object is a concealed projection rather than a real row."""
    return row._ul_concealed  # noqa: SLF001


def writable_wiki(wiki: Wiki) -> Wiki:
    """
    Return a row that may be written: *wiki* itself, or a fresh fetch of it.

    Args:
        wiki: A wiki, possibly a projection.

    Returns:
        A wiki safe to mutate and save."""
    if not is_concealed(wiki):
        return wiki

    return _real_row(wiki)


def redact_edit_changes(changes: Any) -> dict[str, Any]:
    """
    Strip the pre-edit value out of an edit's diff.
    That is a leak a read gate cannot close, because it lands in the viewer's *own* edit row - content the rules promise always to show them.

    Args:
        changes: The edit's stored diff.

    Returns:
        The same shape with every ``from`` replaced."""
    if not isinstance(changes, dict):
        return {}
    redacted: dict[str, Any] = {}
    for field_name, diff in changes.items():
        if isinstance(diff, dict):
            redacted[field_name] = {**diff, "from": None}
        else:
            redacted[field_name] = diff
    return redacted


def visible_article_revision(article: Article, viewer: Profile | None) -> ArticleRevision | None:
    """
    The newest article revision *viewer* is entitled to see, or None.
    An article is entirely user-contributed prose, so unlike a wiki's fields it cannot be resolved write-by-write - there is nothing to merge on.

    Args:
        article: The article being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        The newest revision this viewer may see, or None when there is none - which is what a place nobody has written up looks like."""
    return conceal_rows(article.revisions.all(), viewer).order_by("-created", "-pk").first()


def conceal_article(article: Article | None, wiki: Wiki, viewer: Profile | None) -> Article | None:
    """
    Return *article* itself, a projection of it, or None.
    Returning an empty article instead would be its own tell - the panel renders an always-editable canvas either way, so absence and emptiness look the same to the viewer, but only one of them is honest about what the row contains.

    Args:
        article: The live article row, or None when none exists.
        wiki: The wiki hosting it, for the concealment decision.
        viewer: Who is looking, or None when signed out.

    Returns:
        An article safe to render to this viewer, or None."""
    if article is None or not concealment_active(wiki, viewer):
        return article

    revision = visible_article_revision(article, viewer)
    if revision is None:
        return None

    # The overwhelmingly common case once a viewer has any visible revision:
    # theirs is also the newest, so the live row already says what to show and
    # re-rendering it would be pure cost.
    if revision.content == article.content:
        return article

    from urbanlens.dashboard.services.wiki.articles import render_article

    rendered = render_article(revision.content)
    projection = copy.copy(article)
    projection._state = copy.copy(article._state)  # noqa: SLF001
    projection._state.fields_cache = dict(article._state.fields_cache)  # noqa: SLF001
    projection.content = revision.content
    projection.content_html = rendered.html
    projection.toc = rendered.toc
    projection._ul_concealed = True  # noqa: SLF001
    projection.save = _refuse_write  # type: ignore[method-assign]
    projection.delete = _refuse_write  # type: ignore[method-assign]
    return projection
