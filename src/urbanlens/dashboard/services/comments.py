"""Shared comment visibility, creation, and reaction logic.

Extracted from ``controllers/comments.py`` so the internal HTMX panel and the
external API decide what a viewer may see through **one** implementation.

Why this matters more than a typical de-duplication: a comment is the one place
in the app where a user's text can name a *location they have pinned*, using an
``@[Display](loc:<uuid>)`` token. If a viewer who has not pinned that location
is shown the comment, they learn both that the place exists and that someone
else is interested in it - the exact inference the whole discovery model is
built to prevent. That gate lives in :func:`visible_comment_tree` and nowhere
else; a second caller re-implementing "roughly the same checks" is how it would
quietly regress.

The four gates, applied in this order:

1. ``profile.can_view_comments_from(author)`` - the author's comment-visibility
   setting, cached per unique author.
2. ``pending_scan and author != viewer`` - an image awaiting the async malware
   scan is visible only to its own uploader.
3. ``mentions.is_visible_to`` (via ``render_comment_text`` returning ``None``) -
   a comment mentioning any location the viewer has not pinned is dropped
   **entirely**. Not redacted, not blanked: the existence of the mention is
   itself the leak, so the whole comment disappears.
4. ``mask_profile_references`` - the surviving authors' display identities are
   masked per their profile-visibility settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.identity_visibility import mask_profile_references
from urbanlens.dashboard.services.mentions import extract_location_mentions, render_comment_text, viewer_pinned_uuids

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: The emoji set a client may react with. Anything else is rejected rather than
#: stored, so the reaction column can't be used as free-text storage.
ALLOWED_EMOJIS = {"👍", "👎", "❤️", "😂", "😮", "😢", "🔥", "🏚️"}


class CommentValidationError(ValueError):
    """A comment could not be created as submitted."""


@dataclass(slots=True)
class VisibleComment:
    """One comment that survived every visibility gate, plus its visible replies.

    Attributes:
        comment: The underlying model instance. Its ``profile`` has already had
            masked-identity attributes applied in place.
        rendered_text: The mention-resolved safe HTML for the internal panel.
        replies: Visible direct replies, same shape, one level deep.
        parent_was_deleted: True when this is a reply whose parent was deleted.
            Such a reply also has ``parent=None`` and so queries identically to
            a genuine top-level comment; this distinguishes the two.
    """

    comment: Comment
    rendered_text: str
    parent_was_deleted: bool = False
    replies: list[VisibleComment] = field(default_factory=list)


def top_level_comment_queryset(comments_qs: QuerySet[Comment]) -> QuerySet[Comment]:
    """Narrow a comment queryset to top-level rows with the right joins preloaded.

    Shared so both callers page over an identically-shaped queryset - the
    prefetches here are what keep :func:`visible_comment_tree` from issuing a
    query per comment while it walks replies and reactions.

    Args:
        comments_qs: All comments for one pin or wiki.

    Returns:
        The top-level subset, with author/markup/reaction relations preloaded.
    """
    return (
        comments_qs.filter(parent__isnull=True)
        .select_related("profile__user", "markup_map", "pin", "pin__location")
        .prefetch_related(
            "reactions__profile",
            "replies__reactions__profile",
            "replies__profile__user",
            # comment.map_data derives its snapshot from the markup map's items.
            "markup_map__items",
            "replies__markup_map__items",
        )
    )


def visible_comment_tree(comments: list[Comment], profile: Profile) -> list[VisibleComment]:
    """Filter *comments* to what *profile* may see, resolving mentions.

    Applies the four gates documented in this module's docstring, in that
    order, to both top-level comments and their replies.

    Args:
        comments: Top-level comments to filter - already ordered, prefetched
            (see :func:`top_level_comment_queryset`) and paginated by the
            caller, since the internal panel and the API page differently.
        profile: The viewing profile.

    Returns:
        The visible subset as :class:`VisibleComment` items. A comment dropped
        by any gate is absent entirely, as are its replies.
    """
    pinned = viewer_pinned_uuids(profile)

    # Cache can_view_comments_from per unique author rather than recomputing it
    # once per comment/reply - each call runs up to 3 extra query pairs, which
    # is redundant when one author appears several times in a thread.
    authors: set[Profile] = set()
    for comment in comments:
        authors.add(comment.profile)
        authors.update(reply.profile for reply in comment.replies.all())
    can_view: dict[int, bool] = {author.pk: profile.can_view_comments_from(author) for author in authors}

    # Gate 4. A comment's *content* is all-or-nothing gated by
    # can_view_comments_from below, but once it passes, the author's own
    # name/avatar still need masking per their profile_visibility. Applied to
    # every reference at once so the same author is masked consistently
    # wherever they appear in the thread.
    author_refs: list[Profile] = []
    for comment in comments:
        author_refs.append(comment.profile)
        author_refs.extend(reply.profile for reply in comment.replies.all())
    mask_profile_references(profile, author_refs)

    visible: list[VisibleComment] = []
    for comment in comments:
        rendered = _render_if_visible(comment, profile, pinned, can_view)
        if rendered is None:
            continue

        replies: list[VisibleComment] = []
        for reply in comment.replies.all():
            reply_rendered = _render_if_visible(reply, profile, pinned, can_view)
            if reply_rendered is None:
                continue
            # Replies are one level deep by construction, so their own
            # `replies` list stays empty rather than recursing.
            replies.append(VisibleComment(comment=reply, rendered_text=reply_rendered, parent_was_deleted=reply.parent_deleted))

        visible.append(
            VisibleComment(
                comment=comment,
                rendered_text=rendered,
                parent_was_deleted=comment.parent_deleted,
                replies=replies,
            ),
        )
    return visible


def _render_if_visible(comment: Comment, profile: Profile, pinned: set[Any], can_view: dict[int, bool]) -> str | None:
    """Apply gates 1-3 to one comment.

    Args:
        comment: The comment to test.
        profile: The viewing profile.
        pinned: Location uuids *profile* has pinned.
        can_view: Author-pk to comment-visibility decision (gate 1).

    Returns:
        The rendered HTML when the comment is visible, else ``None``.
    """
    # Gate 1: the author's comment-visibility setting.
    if not can_view.get(comment.profile_id, False):
        return None
    # Gate 2: a newly-uploaded image is scanned asynchronously; until that
    # clears pending_scan the comment is visible only to its own author.
    if comment.pending_scan and comment.profile != profile:
        return None
    # Gate 3: any @loc mention the viewer hasn't pinned drops the whole comment.
    return render_comment_text(comment.text, pinned)


def comment_mentions(text: str) -> list[dict[str, str]]:
    """Resolve the ``@[Display](loc:uuid)`` tokens in *text* to display/slug pairs.

    Only safe to call on text that has already passed
    :func:`visible_comment_tree` - by that point the viewer has provably pinned
    every location mentioned, so naming them reveals nothing new. Calling it on
    unfiltered text would hand back exactly the location identifiers the
    mention gate exists to withhold.

    Args:
        text: Raw comment text in storage format.

    Returns:
        One ``{"display", "location_slug"}`` dict per mention, in order of
        appearance. ``location_slug`` falls back to the uuid when the Location
        row has no slug, since wiki routes resolve either.
    """
    from urbanlens.dashboard.models.location.model import Location

    mentions = extract_location_mentions(text)
    if not mentions:
        return []

    slug_by_uuid = {str(row_uuid): slug for row_uuid, slug in Location.objects.filter(uuid__in=[loc_uuid for _display, loc_uuid in mentions]).values_list("uuid", "slug")}
    return [{"display": display, "location_slug": slug_by_uuid.get(loc_uuid) or loc_uuid} for display, loc_uuid in mentions]


def aggregate_reactions(comment: Comment, profile: Profile) -> dict[str, dict[str, Any]]:
    """Summarize a comment's reactions from the viewer's perspective.

    Args:
        comment: The comment whose reactions to aggregate.
        profile: The viewing profile, used to set ``reacted``.

    Returns:
        ``{emoji: {"count": int, "reacted": bool}}``.
    """
    summary: dict[str, dict[str, Any]] = {}
    for reaction in comment.reactions.all():
        entry = summary.setdefault(reaction.emoji, {"count": 0, "reacted": False})
        entry["count"] += 1
        if reaction.profile_id == profile.pk:
            entry["reacted"] = True
    return summary


def create_comment(*, profile: Profile, pin: Pin | None = None, wiki: Wiki | None = None, text: str, parent: Comment | None = None) -> Comment:
    """Create a comment on a pin or a wiki.

    Args:
        profile: The authoring profile.
        pin: The pin being commented on. Mutually exclusive with *wiki*.
        wiki: The wiki being commented on. Mutually exclusive with *pin*.
        text: The comment body, in storage format (mention tokens intact).
        parent: The comment being replied to, or None for a top-level comment.
            Callers must have already scoped this to the same pin/wiki.

    Returns:
        The created comment.

    Raises:
        CommentValidationError: Neither or both of *pin*/*wiki* were given, or
            *text* is empty after stripping.
    """
    if (pin is None) == (wiki is None):
        raise CommentValidationError("A comment must belong to exactly one of a pin or a wiki.")

    body = (text or "").strip()
    if not body:
        raise CommentValidationError("Comment text cannot be empty.")

    return Comment.objects.create(pin=pin, wiki=wiki, profile=profile, text=body, parent=parent)


def toggle_reaction(profile: Profile, comment: Comment, emoji: str) -> bool:
    """Add *profile*'s reaction to *comment*, or remove it if already present.

    Args:
        profile: The reacting profile.
        comment: The comment being reacted to.
        emoji: One of :data:`ALLOWED_EMOJIS`.

    Returns:
        True when the reaction was added, False when an existing one was
        removed.

    Raises:
        CommentValidationError: *emoji* is not in :data:`ALLOWED_EMOJIS`.
    """
    if emoji not in ALLOWED_EMOJIS:
        raise CommentValidationError("That is not a supported reaction.")

    existing = Reaction.objects.existing(profile, emoji, comment=comment)
    if existing:
        existing.delete()
        return False

    Reaction.objects.create(profile=profile, emoji=emoji, comment=comment)
    return True
