"""Comment and Reaction controllers for Pin and Wiki pages."""

from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.comments.comments import ALLOWED_EMOJIS, CommentValidationError, toggle_reaction, top_level_comment_queryset, visible_comment_tree
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.core.text_limits import MAX_COMMENT_TEXT_LENGTH, text_length_error
from urbanlens.dashboard.services.map.map_snapshot import (
    _sanitize_markup_color,
    _sanitize_markup_shapes,
    _sanitize_number,
    materialize_markup_map,
    parse_map_data as _parse_map_data,
)
from urbanlens.dashboard.services.notifications.comment_notifications import notify_reply
from urbanlens.dashboard.services.notifications.mentions import render_comment_text, viewer_pinned_uuids
from urbanlens.dashboard.services.trips.trip_comments import ALLOWED_COMMENT_EMOJIS
from urbanlens.dashboard.services.undo.handlers.markup_map import MODEL_LABEL as MARKUP_MAP_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, resolve_visible_wiki

# Re-exported so existing imports (e.g. tests) keep resolving from this module.
__all__ = ["_parse_map_data", "_sanitize_markup_color", "_sanitize_markup_shapes", "_sanitize_number"]

logger = logging.getLogger(__name__)

# Canonical definition now lives in services.comments.comments, shared with the external
# API. Aliased here so existing importers (controllers.trip) keep resolving it.
_ALLOWED_EMOJIS = ALLOWED_EMOJIS
_COMMENTS_PAGE_SIZE = 8


def _profile(request) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def comment_image_error(image_file) -> str | None:
    """Validate an image attached to a comment (pin, wiki, or trip) before accepting it.

    Shared by all three comment POST handlers - comments don't go through
    the ``Image`` model, so they can't reuse ``services.media.images.image_upload_error``
    directly, but every upload still gets the same size/content-type checks
    before it's ever saved. The antivirus scan itself is deliberately
    skipped here - it's slow and occasionally unavailable (a clamd hiccup
    used to fail the whole comment submission outright) - and instead runs
    asynchronously after the comment is created (see ``start_comment_image_scan``
    and ``tasks.scan_comment_image``/``scan_trip_comment_image``), with the
    comment hidden from other viewers until it clears.

    Args:
        image_file: The uploaded file from ``request.FILES.get("image")``.

    Returns:
        A user-facing error message if the file should be rejected, or None.
    """
    from urbanlens.dashboard.models.images.model import MediaKind
    from urbanlens.dashboard.services.media.images import image_upload_error

    upload_error = image_upload_error(image_file, MediaKind.PHOTO, skip_malware_scan=True)
    return upload_error[0] if upload_error else None


def start_comment_image_scan(comment) -> None:
    """Mark a newly-uploaded comment image pending and queue its background malware scan.

    Call immediately after saving a brand-new image upload onto a comment
    (never for one attached via "Choose Existing" - that file was already
    scanned on its original upload, see ``attach_existing_comment_image``).
    Sets ``pending_scan`` so the comment is hidden from every other viewer
    (see ``_build_context``/``trip._render_trip_comments``) until the scan
    clears it, then enqueues the appropriate task for whichever comment type
    this is.

    Args:
        comment: The just-created ``Comment`` or ``TripComment``, already
            carrying its new image.
    """
    from urbanlens.dashboard.models.trips.model import TripComment
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import scan_comment_image, scan_trip_comment_image

    comment.pending_scan = True
    comment.save(update_fields=["pending_scan"])
    task = scan_trip_comment_image if isinstance(comment, TripComment) else scan_comment_image
    safely_enqueue_task(task, comment.pk)


def _discard_comment_image(comment) -> None:
    """Remove a deleted comment's stored photo from disk.

    Django stopped deleting ``FileField`` files on row deletion in 1.3, so a
    deleted comment used to leave its photo under ``comment_images/`` forever -
    where the media gate's orphan branch serves it to any authenticated user
    who knows the name (see "Authenticated media gate - residual per-family
    risk" in docs/PROBLEMS.md). Each comment owns its file outright:
    :func:`attach_existing_comment_image` *copies* rather than sharing storage,
    precisely so one deletion cannot strand another row's image.

    Best-effort - the row is already gone, and a storage hiccup must not turn a
    successful delete into a 500.

    Args:
        comment: The comment whose file should be discarded. Safe to call when
            it has no image.
    """
    if not comment.image:
        return
    try:
        comment.image.delete(save=False)
    except OSError:
        logger.warning("Could not delete stored image for deleted comment %s", comment.pk, exc_info=True)


def attach_existing_comment_image(comment: Comment, existing_image_id: str, profile: Profile) -> None:
    """Copy one of the poster's own already-uploaded photos onto a comment.

    Backs the "Choose Existing" tab of the comment/Notes image-attach dialog
    (base.html's ``_openCommentAttachImageDialog``), so posting a photo
    already shared elsewhere doesn't require re-uploading a duplicate. Copies
    the file rather than pointing the comment at the same storage the source
    ``Image`` uses, so deleting either later doesn't orphan the other -
    deliberately skips re-running ``comment_image_error`` too, since the
    source file already passed those checks (size/content-type/malware) on
    its original upload.

    Args:
        comment: The already-created comment to attach the photo to.
        existing_image_id: The ``Image.pk`` submitted by the picker.
        profile: The poster - only their own photos are eligible, same scope
            ``CommentImagePickerView`` lists.

    Silently no-ops on a bad/foreign id rather than failing the whole post -
    it only ever comes from a picker listing the poster's own photos, so a
    mismatch means stale client state, not something worth a hard error.
    """
    import os

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image

    source = Image.objects.uploaded_by(profile).filter(pk=existing_image_id).first()
    if not source:
        return
    comment.image.save(os.path.basename(source.image.name), ContentFile(source.image.read()), save=True)


class CommentImagePickerView(LoginRequiredMixin, View):
    """GET /comments/images/picker/ - list the caller's own uploaded photos to attach.

    Companion to the plain upload flow for comment/Notes image attachments
    (``#comment-image-composer``'s "Choose Existing" tab): lets the poster
    reuse one of their own photos instead of uploading a duplicate. Entirely
    generic - just the caller's own ``Image`` rows, no comment-specific
    filtering - mirroring how ``DirectMessageMapPickerView`` is reused as-is
    for the analogous "Choose Existing" tab on the map-attach dialog.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the picker list of the caller's own photo uploads.

        Args:
            request: The HTTP request, optionally carrying a ``q`` search term.

        Returns:
            Rendered HTML fragment listing matching photos.
        """
        from urbanlens.dashboard.models.images.model import Image, MediaKind

        profile = _profile(request)
        query = (request.GET.get("q") or "").strip()
        candidates = Image.objects.uploaded_by(profile).filter(media_type=MediaKind.PHOTO)
        if query:
            candidates = candidates.filter(caption__icontains=query)
        return render(request, "dashboard/partials/comments/_comment_image_picker.html", {"candidates": candidates[:50], "query": query})


def _render_comments(request, context: dict) -> HttpResponse:
    return render(request, "dashboard/partials/comments/comment_panel.html", context)


def _build_context(comments_qs, profile: Profile, request: HttpRequest, **extra) -> dict:
    top_level_qs = top_level_comment_queryset(comments_qs)
    # Default to the last page so the most recent activity (comments are
    # ordered oldest-to-newest) is what a viewer sees without paging back.
    page_obj = get_page(request, top_level_qs, _COMMENTS_PAGE_SIZE, default_last=True)
    top_level = list(page_obj.object_list)

    # Collect all unique commenter profiles so we can check photo visibility once.
    all_commenters: set[Profile] = set()
    for c in top_level:
        all_commenters.add(c.profile)
        all_commenters.update(r.profile for r in c.replies.all())
    # Set of profile IDs whose images should be blurred for this viewer.
    blurred_profiles: set[int] = {p.pk for p in all_commenters if p != profile and not profile.can_view_photos_from(p)}

    # Every visibility decision - comment-visibility, pending-scan, the @loc
    # mention gate, and author-identity masking - lives in this one call, shared
    # with the external API so the two surfaces cannot drift apart on it.
    visible = visible_comment_tree(top_level, profile)

    rendered = [
        {
            "comment": item.comment,
            "rendered_text": item.rendered_text,
            "reactions": _aggregate_reactions(item.comment.reactions.all()),
            "replies": [
                {
                    "comment": reply.comment,
                    "rendered_text": reply.rendered_text,
                    "reactions": _aggregate_reactions(reply.comment.reactions.all()),
                }
                for reply in item.replies
            ],
            # A reply whose parent was deleted also has parent=None, so it
            # queries identically to a genuine top-level comment - this
            # distinguishes the two so the template can render an "[Original
            # comment deleted]" placeholder above it instead of showing it as
            # if it had always stood on its own (UL-219).
            "parent_was_deleted": item.parent_was_deleted,
        }
        for item in visible
    ]
    return {
        "rendered_comments": rendered,
        "page_obj": page_obj,
        "total_comment_count": comments_qs.count(),
        "profile": profile,
        "blurred_profiles": blurred_profiles,
        "allowed_emojis": sorted(_ALLOWED_EMOJIS),
        **extra,
    }


# -- Pin comments --------------------------------------------------------------


def _pin_comments_context(pin, profile: Profile, request: HttpRequest) -> dict:
    """Build the Notes panel context for a pin, aggregating child pins' notes on ``?children=1``.

    Mirrors the page-wide "show child pin details" toggle already applied to the
    map, photo gallery, and visit history (see ``controllers.visits._render_visit_history``)
    - a note left on a child pin must not be invisible from the parent's own
    Notes tab just because it happens to live on a nested row. Posting and
    deleting still always act on the exact pin passed in; only the *listing*
    aggregates.

    Args:
        pin: The pin whose Notes panel is being rendered.
        profile: The viewing profile (always the pin's owner - notes are private).
        request: The current request, read for the ``children`` flag and pagination.

    Returns:
        The context dict for ``comment_panel.html``.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    include_children = request.GET.get("children") == "1"
    if include_children:
        subtree = Pin.objects.filter(pk=pin.pk).with_descendants()
        comments_qs = Comment.objects.filter(pin__in=subtree)
    else:
        comments_qs = pin.comments.all()
    return _build_context(
        comments_qs,
        profile,
        request,
        pin=pin,
        context_type="pin",
        include_children=include_children,
        extra_query="children=1" if include_children else "",
    )


class PinCommentsView(LoginRequiredMixin, View):
    """GET/POST comment panel for a Pin."""

    def get(self, request, pin_slug):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        ctx = _pin_comments_context(pin, profile, request)
        return _render_comments(request, ctx)

    def post(self, request, pin_slug):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        text = request.POST.get("text", "").strip()
        image = request.FILES.get("image")
        existing_image_id = request.POST.get("existing_image_id", "").strip()
        map_data = _parse_map_data(request)
        if not text and not image and not existing_image_id and not map_data:
            return HttpResponse("Please add some text, a photo, or a map.", status=400)
        length_error = text_length_error(text, MAX_COMMENT_TEXT_LENGTH, "Comment")
        if length_error:
            return HttpResponse(length_error, status=400)
        if image and (image_error := comment_image_error(image)):
            return HttpResponse(image_error, status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, pin=pin)
        comment = Comment.objects.create(pin=pin, profile=profile, text=text, parent=parent, markup_map=materialize_markup_map(profile, map_data, context=pin))
        if image:
            comment.image = image
            comment.save(update_fields=["image"])
            start_comment_image_scan(comment)
        elif existing_image_id:
            attach_existing_comment_image(comment, existing_image_id, profile)
        if parent and parent.profile != profile:
            notify_reply(profile, parent, reply=comment)
        ctx = _pin_comments_context(pin, profile, request)
        return _render_comments(request, ctx)


class PinCommentDeleteView(LoginRequiredMixin, View):
    """DELETE /map/pin/<uuid>/comments/<int>/delete/"""

    def delete(self, request, pin_slug, comment_id):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        # A deletable comment may live on any descendant, not just the exact
        # pin in the URL - the aggregated view's delete buttons post back here
        # for a note authored on a child pin.
        subtree = Pin.objects.filter(pk=pin.pk).with_descendants()
        comment = get_object_or_404(Comment, id=comment_id, pin__in=subtree)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        markup_map = comment.markup_map
        comment.delete()
        _discard_comment_image(comment)
        if markup_map is not None:
            # The comment itself is not restorable, but the map attached to it is
            # hand-drawn work - stash it so deleting the comment can't silently
            # destroy the drawing.
            stash_for_undo(MARKUP_MAP_MODEL_LABEL, [markup_map], markup_map.profile)
            markup_map.delete()
        # Replies to a deleted comment survive (parent FK is SET_NULL), becoming
        # orphaned top-level comments. Re-render the whole panel rather than just
        # removing the deleted <li>, so those replies stay visible in place
        # instead of disappearing until the next reload.
        ctx = _pin_comments_context(pin, profile, request)
        return _render_comments(request, ctx)


# -- Wiki comments -------------------------------------------------------------


class WikiCommentsView(LoginRequiredMixin, View):
    """GET/POST comment panel for a wiki."""

    def get(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        ctx = _build_context(wiki.comments.all(), profile, request, wiki=wiki, location=wiki.location, context_type="wiki")
        return _render_comments(request, ctx)

    def post(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        text = request.POST.get("text", "").strip()
        image = request.FILES.get("image")
        existing_image_id = request.POST.get("existing_image_id", "").strip()
        map_data = _parse_map_data(request)
        if not text and not image and not existing_image_id and not map_data:
            return HttpResponse("Please add some text, a photo, or a map.", status=400)
        length_error = text_length_error(text, MAX_COMMENT_TEXT_LENGTH, "Comment")
        if length_error:
            return HttpResponse(length_error, status=400)
        if image and (image_error := comment_image_error(image)):
            return HttpResponse(image_error, status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, wiki=wiki)
        comment = Comment.objects.create(
            wiki=wiki,
            profile=profile,
            text=text,
            parent=parent,
            markup_map=materialize_markup_map(profile, map_data, context=wiki),
        )
        if image:
            comment.image = image
            comment.save(update_fields=["image"])
            start_comment_image_scan(comment)
        elif existing_image_id:
            attach_existing_comment_image(comment, existing_image_id, profile)
        if parent and parent.profile != profile:
            notify_reply(profile, parent, reply=comment)
        ctx = _build_context(wiki.comments.all(), profile, request, wiki=wiki, location=wiki.location, context_type="wiki")
        return _render_comments(request, ctx)


class WikiCommentDeleteView(LoginRequiredMixin, View):
    """DELETE /location/<slug>/wiki/comments/<int>/delete/"""

    def delete(self, request, location_slug, comment_id):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        comment = get_object_or_404(Comment, id=comment_id, wiki=wiki)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        markup_map = comment.markup_map
        comment.delete()
        _discard_comment_image(comment)
        if markup_map is not None:
            # The comment itself is not restorable, but the map attached to it is
            # hand-drawn work - stash it so deleting the comment can't silently
            # destroy the drawing.
            stash_for_undo(MARKUP_MAP_MODEL_LABEL, [markup_map], markup_map.profile)
            markup_map.delete()
        # Replies to a deleted comment survive (parent FK is SET_NULL), becoming
        # orphaned top-level comments. Re-render the whole panel rather than just
        # removing the deleted <li>, so those replies stay visible in place
        # instead of disappearing until the next reload.
        ctx = _build_context(wiki.comments.all(), profile, request, wiki=wiki, location=wiki.location, context_type="wiki")
        return _render_comments(request, ctx)


# -- Reactions ------------------------------------------------------------------


class CommentReactionView(LoginRequiredMixin, View):
    """POST /comments/<int>/react/  - toggle an emoji reaction on a Comment."""

    def post(self, request, comment_id):
        profile = _profile(request)
        # Only comments the user can actually see: comments on their own pins,
        # or on wikis for locations they have pinned themselves. An unscoped
        # id lookup would let sequential-id probing react to (and read
        # reaction rows of) comments on private pins or wikis the requester
        # can't otherwise view.
        comment = get_object_or_404(
            Comment.objects.filter(Q(pin__profile=profile) | Q(wiki__isnull=False)).select_related("wiki__location", "profile"),
            id=comment_id,
        )
        if comment.wiki_id and not location_visible_to(comment.wiki.location, profile):
            raise Http404
        # Page-level visibility isn't enough on its own - the comment's own
        # author may further restrict who can see (and react to) it via their
        # comment_visibility privacy setting.
        if not profile.can_view_comments_from(comment.profile):
            raise Http404
        # The add/remove/notify sequence lives in the service, not here. It used
        # to be hand-rolled in this view as well, and the two copies agreeing was
        # a coincidence: fixing a missing notification on the service side (the
        # external API reacted without ever notifying the author) left this copy
        # untouched, and only the panel's copy happened to already be correct.
        try:
            toggle_reaction(profile, comment, request.POST.get("emoji", ""))
        except CommentValidationError:
            return HttpResponse("Invalid emoji.", status=400)
        return _render_reaction_row(request, comment, profile)


class TripCommentReactionView(LoginRequiredMixin, View):
    """POST /trips/<slug>/comments/<int>/react/  - toggle reaction on a TripComment."""

    def post(self, request, trip_slug, comment_id):
        from urbanlens.dashboard.services.trips.trip_access import get_trip_for_viewer
        from urbanlens.dashboard.services.trips.trip_comments import get_comment, set_comment_reaction
        from urbanlens.dashboard.services.trips.trip_errors import TripError, TripNotFoundError, TripPermissionError

        profile = _profile(request)
        try:
            trip = get_trip_for_viewer(trip_slug, profile)
            comment = get_comment(trip, comment_id)
            emoji = request.POST.get("emoji", "")
            # The panel keeps its toggle UX; the service takes an explicit target
            # state so a retried API call can't silently undo itself.
            already = Reaction.objects.existing(profile, emoji, trip_comment=comment) is not None if emoji in ALLOWED_COMMENT_EMOJIS else False
            set_comment_reaction(comment, profile, emoji, reacted=not already)
        except TripNotFoundError as exc:
            raise Http404(exc.message) from exc
        except TripError as exc:
            return HttpResponse(exc.message, status=403 if isinstance(exc, TripPermissionError) else 400)
        return _render_trip_reaction_row(request, comment, profile)


def _render_reaction_row(request, comment: Comment, profile: Profile) -> HttpResponse:
    reactions = _aggregate_reactions(comment.reactions.all())
    return render(
        request,
        "dashboard/partials/comments/comment_reactions.html",
        {
            "comment": comment,
            "reactions": reactions,
            "profile": profile,
            "react_url_name": "comment.react",
            "allowed_emojis": _ALLOWED_EMOJIS,
        },
    )


def _render_trip_reaction_row(request, comment, profile: Profile) -> HttpResponse:
    reactions = _aggregate_reactions(comment.reactions.all())
    return render(
        request,
        "dashboard/partials/comments/comment_reactions.html",
        {
            "comment": comment,
            "reactions": reactions,
            "profile": profile,
            "react_url_name": "trips.comment.react",
            "trip_slug": comment.trip.slug,
            "allowed_emojis": _ALLOWED_EMOJIS,
        },
    )


class _ReactionData(TypedDict):
    count: int
    reacted_by: list[int]


def _aggregate_reactions(reactions_qs) -> dict[str, _ReactionData]:
    """Group reactions by emoji → {count, reacted_by: list of profile_ids}."""
    result: dict[str, _ReactionData] = {}
    for r in reactions_qs.select_related("profile"):
        if r.emoji not in result:
            result[r.emoji] = {"count": 0, "reacted_by": []}
        result[r.emoji]["count"] += 1
        result[r.emoji]["reacted_by"].append(r.profile_id)
    return result


# -- Location autocomplete endpoint -------------------------------------------


class PinnedLocationsJsonView(LoginRequiredMixin, View):
    """GET /comments/locations/  - return viewer's pinned locations for @mention autocomplete."""

    def get(self, request):

        from urbanlens.dashboard.models.pin.model import Pin

        profile = _profile(request)
        q = request.GET.get("q", "").strip().lower()
        pins = Pin.objects.filter(profile=profile).exclude(location__isnull=True).select_related("location__wiki")[:50]
        results = []
        for pin in pins:
            name = pin.location.display_name or ""
            if not q or q in name.lower():
                results.append({"uuid": str(pin.location.uuid), "name": name})
        return HttpResponse(json.dumps(results), content_type="application/json")
