"""Trip comment visibility and mutation, shared by the panel and the REST API.

The visible tree is built once, here, so both surfaces apply the same three
independent gates in the same order:

1. the author's ``comment_visibility`` hides the whole comment from viewers
   they don't allow (all-or-nothing),
2. an image still awaiting its background malware scan (``pending_scan``)
   keeps the comment visible only to its own author,
3. mention rendering can itself decline to render (a mention of a pin the
   viewer can't see), which drops the comment.

Once a comment passes those, the author's *profile* visibility separately
masks their name and avatar while the content stays visible - see
``services.identity_visibility``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from urbanlens.dashboard.models.trips.model import Trip, TripComment

# Module-level, unlike the controller helpers below: these notifications are a
# service now, so importing them no longer drags a view layer in and no longer
# risks the import cycle the function-local imports were working around.
from urbanlens.dashboard.services.comment_notifications import notify_reaction, notify_reply
from urbanlens.dashboard.services.comments import ALLOWED_EMOJIS
from urbanlens.dashboard.services.text_limits import MAX_COMMENT_TEXT_LENGTH, text_length_error
from urbanlens.dashboard.services.trip_access import require_perform
from urbanlens.dashboard.services.trip_errors import TripNotFoundError, TripPermissionError, TripValidationError

if TYPE_CHECKING:
    from urbanlens.dashboard.controllers.comments import _ReactionData
    from urbanlens.dashboard.models.profile.model import Profile


class TripReplyData(TypedDict):
    """One visible reply, as :func:`build_comment_tree` yields it."""

    comment: TripComment
    rendered_text: str
    reactions: dict[str, _ReactionData]
    can_delete: bool


class TripCommentData(TripReplyData):
    """One visible top-level comment, with its own visible replies."""

    replies: list[TripReplyData]

#: The reactions a trip (or pin, or wiki) comment may carry. Re-exported from
#: ``services.comments`` (the single source of truth for the set) so the
#: external API's trip serializers can bound their ``emoji`` field without
#: importing a controller.
ALLOWED_COMMENT_EMOJIS = frozenset(ALLOWED_EMOJIS)

COMMENT_DENIED = "You don't have permission to comment on this trip."
COMMENT_DELETE_DENIED = "You can only delete your own comments."
COMMENT_NOT_FOUND = "No such comment."
EMPTY_COMMENT = "Please add some text, a photo, or a map."


def get_comment(trip: Trip, comment_id: int) -> TripComment:
    """Return one of the trip's own comments.

    Args:
        trip: The trip that must own the comment.
        comment_id: The comment's primary key.

    Returns:
        The comment.

    Raises:
        TripNotFoundError: No such comment on this trip.
    """
    comment = TripComment.objects.filter(id=comment_id, trip=trip).select_related("author__user", "markup_map").first()
    if comment is None:
        raise TripNotFoundError(COMMENT_NOT_FOUND)
    return comment


def can_delete_comment(comment: TripComment, viewer: Profile, trip: Trip) -> bool:
    """Return True when *viewer* may delete *comment*.

    Args:
        comment: The comment in question.
        viewer: The profile asking.
        trip: The comment's trip (for the creator override).

    Returns:
        True for the comment's own author and for the trip's creator.
    """
    return viewer.id in {comment.author_id, trip.creator_id}


def build_comment_tree(trip: Trip, viewer: Profile) -> list[TripCommentData]:
    """Build the trip's visible comment tree for one viewer.

    Args:
        trip: The trip whose comments are wanted.
        viewer: The profile reading them.

    Returns:
        Top-level comments in creation order, each a dict with ``comment``,
        ``rendered_text`` (mention-rendered HTML), ``reactions``
        (``{emoji: {count, reacted_by}}``), ``can_delete`` and ``replies`` -
        each reply carrying the same keys minus ``replies``. Comments the
        viewer may not see are absent entirely.
    """
    from urbanlens.dashboard.controllers.comments import _aggregate_reactions
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities
    from urbanlens.dashboard.services.mentions import render_comment_text, viewer_pinned_uuids
    from urbanlens.dashboard.services.trip_activities import activity_queryset, compute_activity_index_map

    activities = list(activity_queryset(trip))
    index_map = compute_activity_index_map(activities)
    act_by_index = {v: a for a, v in index_map.items()}
    act_objects = {a.id: a for a in activities}
    act_index_for_render = {idx: act_objects[act_id] for idx, act_id in act_by_index.items()}

    pinned = viewer_pinned_uuids(viewer)
    top_comments = list(
        trip.comments.filter(parent__isnull=True)
        .select_related("author__user", "markup_map")
        # comment.map_data derives its snapshot from the markup map's items.
        .prefetch_related("reactions", "replies__reactions", "replies__author__user", "markup_map__items", "replies__markup_map__items")
        .order_by("created"),
    )

    # select_related gives each comment/reply its own author instance even for
    # the same underlying profile, so resolve once per distinct author and
    # re-point every comment/reply at that same (now-mutated) instance.
    distinct_authors: dict[int, Profile] = {}
    for c in top_comments:
        if c.author is not None:
            distinct_authors[c.author.pk] = c.author
        for r in c.replies.all():
            if r.author is not None:
                distinct_authors[r.author.pk] = r.author
    if distinct_authors:
        resolve_visible_identities(viewer, list(distinct_authors.values()))
        for c in top_comments:
            if c.author is not None:
                c.author = distinct_authors[c.author.pk]
            for r in c.replies.all():
                if r.author is not None:
                    r.author = distinct_authors[r.author.pk]

    rendered: list[TripCommentData] = []
    for c in top_comments:
        # The author's comment_visibility gates the whole comment for this
        # viewer, exactly as pin/wiki comments already do. A comment whose
        # author was deleted has no visibility preference left to enforce.
        if c.author is not None and not viewer.can_view_comments_from(c.author):
            continue
        # A newly-uploaded image is scanned asynchronously - until that clears
        # pending_scan, the comment stays visible only to its own author.
        if c.pending_scan and c.author != viewer:
            continue
        html = render_comment_text(c.text, pinned, act_index_for_render)
        if html is None:
            continue
        reactions = _aggregate_reactions(c.reactions.all())
        replies_rendered: list[TripReplyData] = []
        for r in c.replies.all():
            if r.author is not None and not viewer.can_view_comments_from(r.author):
                continue
            if r.pending_scan and r.author != viewer:
                continue
            r_html = render_comment_text(r.text, pinned, act_index_for_render)
            if r_html is None:
                continue
            replies_rendered.append(
                {
                    "comment": r,
                    "rendered_text": r_html,
                    "reactions": _aggregate_reactions(r.reactions.all()),
                    "can_delete": can_delete_comment(r, viewer, trip),
                },
            )
        rendered.append(
            {
                "comment": c,
                "rendered_text": html,
                "reactions": reactions,
                "can_delete": can_delete_comment(c, viewer, trip),
                "replies": replies_rendered,
            },
        )
    return rendered


def add_comment(
    trip: Trip,
    actor: Profile,
    *,
    text: str = "",
    parent_id: Any = None,
    image: Any = None,
    existing_image_id: str = "",
    map_data: Any = None,
) -> TripComment:
    """Post a comment (or a reply) on a trip.

    Args:
        trip: The trip being commented on.
        actor: The commenting profile.
        text: The comment body; may be blank when an image or map is attached.
        parent_id: The id of a comment on this same trip to reply to.
        image: A newly uploaded image file, validated and then scanned in the
            background before other members can see the comment.
        existing_image_id: The id of one of the actor's own already-uploaded
            photos to attach instead of a fresh upload.
        map_data: A parsed markup-map payload to materialize and attach.

    Returns:
        The created comment.

    Raises:
        TripPermissionError: The actor may not comment on this trip.
        TripValidationError: Nothing was submitted, the text exceeds the
            shared limit, or the image was rejected.
        TripNotFoundError: ``parent_id`` is not a comment on this trip.
    """
    from urbanlens.dashboard.controllers.comments import attach_existing_comment_image, comment_image_error, start_comment_image_scan
    from urbanlens.dashboard.services.map_snapshot import materialize_markup_map

    require_perform(actor, trip, trip.allow_comments, COMMENT_DENIED)

    clean_text = (text or "").strip()
    if not clean_text and not image and not existing_image_id and not map_data:
        raise TripValidationError(EMPTY_COMMENT)
    length_error = text_length_error(clean_text, MAX_COMMENT_TEXT_LENGTH, "Comment")
    if length_error:
        raise TripValidationError(length_error)
    if image and (image_error := comment_image_error(image)):
        raise TripValidationError(image_error)

    parent = None
    if parent_id:
        parent = TripComment.objects.filter(id=parent_id, trip=trip).select_related("author").first()
        if parent is None:
            raise TripNotFoundError(COMMENT_NOT_FOUND)

    comment = TripComment.objects.create(
        trip=trip,
        author=actor,
        text=clean_text,
        parent=parent,
        markup_map=materialize_markup_map(actor, map_data, context=trip),
    )
    if image:
        comment.image = image
        comment.save(update_fields=["image"])
        start_comment_image_scan(comment)
    elif existing_image_id:
        attach_existing_comment_image(comment, existing_image_id, actor)

    if parent and parent.author and parent.author != actor:
        notify_reply(actor, parent, reply=comment)
    return comment


def delete_comment(trip: Trip, actor: Profile, comment: TripComment) -> None:
    """Delete a trip comment, along with any markup map it carried.

    Args:
        trip: The trip owning the comment.
        actor: The profile deleting it.
        comment: The comment to delete.

    Raises:
        TripPermissionError: The actor is neither the comment's author nor the
            trip's creator.
    """
    if not can_delete_comment(comment, actor, trip):
        raise TripPermissionError(COMMENT_DELETE_DENIED)
    markup_map = comment.markup_map
    comment.delete()
    if markup_map is not None:
        markup_map.delete()


def _comment_visible_to(comment: TripComment, viewer: Profile) -> bool:
    """Whether one trip comment survives every gate :func:`build_comment_tree` applies.

    The single-comment counterpart to that function, for the paths that address
    a comment by id rather than rendering the panel. It must stay in step with
    the three gates in the tree builder's loop - author comment-visibility, the
    pending malware scan, and mention rendering - because a caller reaching a
    comment by id that the tree would have dropped can both confirm the id
    exists and act on it.

    Args:
        comment: The comment being addressed.
        viewer: The profile acting on it.

    Returns:
        True when ``viewer`` would have been shown this comment.
    """
    from urbanlens.dashboard.services.mentions import render_comment_text, viewer_pinned_uuids
    from urbanlens.dashboard.services.trip_activities import activity_queryset, compute_activity_index_map

    if comment.author is not None and not viewer.can_view_comments_from(comment.author):
        return False
    if comment.pending_scan and comment.author != viewer:
        return False

    # Mentions resolve against the comment's own trip, so the activity index
    # map has to be rebuilt for it - an @activity token renders only when that
    # activity is on this trip, and render_comment_text returns None otherwise.
    trip = comment.trip
    activities = list(activity_queryset(trip))
    index_map = compute_activity_index_map(activities)
    act_objects = {activity.id: activity for activity in activities}
    act_index_for_render = {index: act_objects[activity_id] for activity_id, index in index_map.items()}
    return render_comment_text(comment.text, viewer_pinned_uuids(viewer), act_index_for_render) is not None


def set_comment_reaction(comment: TripComment, profile: Profile, emoji: str, *, reacted: bool) -> None:
    """Add or remove one emoji reaction on a trip comment.

    An explicit target state rather than a toggle, so a retried request can't
    silently undo itself. The internal panel keeps its toggle UX by passing
    ``reacted=not already_reacted``.

    Args:
        comment: The comment being reacted to.
        profile: The reacting profile.
        emoji: One of :data:`ALLOWED_COMMENT_EMOJIS`.
        reacted: True to ensure the reaction exists, False to ensure it doesn't.

    Raises:
        TripValidationError: The emoji is not one of the allowed reactions.
        TripNotFoundError: Any of the gates :func:`build_comment_tree` applies
            hides this comment from the reacting profile. Reported as "not
            found" rather than "forbidden" so reacting can't be used to probe
            for comments the viewer was never shown.
    """
    from urbanlens.dashboard.models.reactions.model import Reaction

    if emoji not in ALLOWED_COMMENT_EMOJIS:
        raise TripValidationError("Invalid emoji.")
    # *All* the gates the panel render applies, not just the first. Checking
    # only comment_visibility left the other two reachable by id: a comment
    # whose image is still pending_scan, and one naming a trip activity or
    # @loc the viewer can't resolve, are both dropped from build_comment_tree
    # but were still reactable. That let a member with a guessed sequential id
    # confirm the comment exists and fire a reaction notification at its
    # author - the exact probe this function's "not found" answer exists to
    # prevent.
    if not _comment_visible_to(comment, profile):
        raise TripNotFoundError(COMMENT_NOT_FOUND)

    existing = Reaction.objects.existing(profile, emoji, trip_comment=comment)
    if reacted and not existing:
        Reaction.objects.create(profile=profile, emoji=emoji, trip_comment=comment)
        notify_reaction(profile, comment)
    elif not reacted and existing:
        existing.delete()
