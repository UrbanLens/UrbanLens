"""Comment and Reaction controllers for Pin and Wiki pages."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
from typing import TYPE_CHECKING, Any, TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.comments.comments import ALLOWED_EMOJIS, UnsupportedReactionEmojiError, comment_is_visible, toggle_reaction, top_level_comment_queryset, visible_comment_count, visible_comment_tree
from urbanlens.dashboard.services.core.child_details import child_details_requested
from urbanlens.dashboard.services.core.numbers import safe_int_or_none
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.core.text_limits import MAX_COMMENT_TEXT_LENGTH, text_length_error
from urbanlens.dashboard.services.map.map_snapshot import (
    _sanitize_markup_color,
    _sanitize_markup_shapes,
    _sanitize_number,
    materialize_markup_map,
    parse_map_data as _parse_map_data,
)
from urbanlens.dashboard.services.media.storage import StorageUnavailableError, storage_failures_refused
from urbanlens.dashboard.services.notifications.comment_notifications import notify_reply
from urbanlens.dashboard.services.trips.trip_comments import ALLOWED_COMMENT_EMOJIS
from urbanlens.dashboard.services.undo.handlers.markup_map import MODEL_LABEL as MARKUP_MAP_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki.viewer_projection import viewer_projection, visible_rows
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, resolve_visible_wiki

# Re-exported so existing imports (e.g. tests) keep resolving from this module.
__all__ = ["_parse_map_data", "_sanitize_markup_color", "_sanitize_markup_shapes", "_sanitize_number"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.trips.model import TripComment
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.viewer_projection import ViewerProjection

logger = logging.getLogger(__name__)

# Canonical definition now lives in services.comments.comments, shared with the external
# API. Aliased here so existing importers (controllers.trip) keep resolving it.
_ALLOWED_EMOJIS = ALLOWED_EMOJIS
_COMMENTS_PAGE_SIZE = 8


def _profile(request) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def comment_image_error(image_file) -> str | None:
    """
    Validate an image attached to a comment before accepting it.

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
    """
    Mark a newly-uploaded comment image pending and queue its background malware scan.

    Call immediately after saving a brand-new image upload onto a comment (never for one attached via
    "Choose Existing" - that file was already scanned on its original upload, see
    ``attach_existing_comment_image``).

    Args:
        comment: The just-created ``Comment`` or ``TripComment``, already carrying its new image.
    """
    from urbanlens.dashboard.models.trips.model import TripComment
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import scan_comment_image, scan_trip_comment_image

    comment.pending_scan = True
    comment.save(update_fields=["pending_scan"])
    task = scan_trip_comment_image if isinstance(comment, TripComment) else scan_comment_image
    safely_enqueue_task(task, comment.pk)


def _discard_comment_image(comment) -> None:
    """
    Remove a deleted comment's stored photo from disk.

    Each comment owns its file outright: :func:`attach_existing_comment_image` *copies* rather than
    sharing storage, precisely so one deletion cannot strand another row's image.

    Args:
        comment: The comment whose file should be discarded.
    """
    if not comment.image:
        return
    try:
        comment.image.delete(save=False)
    except OSError:
        logger.warning("Could not delete stored image for deleted comment %s", comment.pk, exc_info=True)


def existing_image_error(existing_image_id: str, profile: Profile) -> str | None:
    """
    Refuse a "Choose Existing" photo that is still the unprocessed upload.

    Args:
        existing_image_id: The ``Image.pk`` submitted by the picker, or "".
        profile: The poster.

    Returns:
        A message when the photo is one of the poster's still being processed, else None.
    """
    from urbanlens.dashboard.models.images.model import Image

    if existing_image_id and Image.objects.uploaded_by(profile).filter(pk=safe_int_or_none(existing_image_id), pending_scan=True).exists():
        return "That photo is still being processed. Try again in a moment."
    return None


def attach_existing_comment_image(comment: Comment | TripComment, existing_image_id: str, profile: Profile) -> None:
    """
    Copy one of the poster's own already-uploaded photos onto a comment.

    Copies the file rather than pointing the comment at the same storage the source ``Image`` uses, so
    deleting either later doesn't orphan the other - deliberately skips re-running
    ``comment_image_error`` too, since the source file already passed those checks
    (size/content-type/malware) on its original upload.

    Args:
        comment: The already-created comment to attach the photo to.
        existing_image_id: The ``Image.pk`` submitted by the picker.
        profile: The poster - only their own photos are eligible, same scope ``CommentImagePickerView``
        lists.
    """
    import os

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image, MediaKind

    # servable(): a pending photo's file is the raw upload, unscanned and with its metadata.
    source = Image.objects.uploaded_by(profile).servable().filter(pk=safe_int_or_none(existing_image_id), media_type=MediaKind.PHOTO).first()
    if not source or not source.image.name:
        return
    comment.image.save(os.path.basename(source.image.name), ContentFile(source.image.read()), save=True)


def create_comment_with_image[C: (Comment, TripComment)](create: Callable[[], C], image: UploadedFile | None, existing_image_id: str, profile: Profile) -> C:
    """
    Create a comment and store its photo in one transaction, so a photo storage refuses leaves no comment behind.

    Args:
        create: Inserts the comment.
        image: A newly uploaded photo, already validated by :func:`comment_image_error`.
        existing_image_id: The ``Image.pk`` of one of the poster's photos to copy on instead, or "".
        profile: The poster.

    Returns:
        The comment.

    Raises:
        StorageUnavailableError: Storage could not store the photo, and nothing was saved.
    """
    with storage_failures_refused(), transaction.atomic():
        comment = create()
        if image:
            comment.image = image
            # In the write that stores the image, so no committed state shows the upload as sent.
            comment.pending_scan = True
            comment.save(update_fields=["image", "pending_scan"])
        elif existing_image_id:
            attach_existing_comment_image(comment, existing_image_id, profile)
    if image:
        start_comment_image_scan(comment)
    return comment


class CommentImagePickerView(LoginRequiredMixin, View):
    """
    GET /comments/images/picker/ - list the caller's own uploaded photos to attach.

    Companion to the plain upload flow for comment/Notes image attachments
    (``#comment-image-composer``'s "Choose Existing" tab): lets the poster reuse one of their own photos
    instead of uploading a duplicate.
    Entirely generic - just the caller's own ``Image`` rows, no comment-specific filtering - mirroring
    how ``DirectMessageMapPickerView`` is reused as-is for the analogous "Choose Existing" tab on the
    map-attach dialog.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """
        Render the picker list of the caller's own photo uploads.

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


#: Matches ``shared/photo-processing.ts``'s ``MAX_IDS_PER_POLL``.
_PROCESSING_STATUS_MAX_IDS = 100


class CommentImageProcessingView(LoginRequiredMixin, View, ABC):
    """
    Which of the requester's own comment images have finished processing.

    GET ...?ids=1,2,3 - polled by the author's placeholder (``_comment_body.html``) until the re-encode,
    which replaces the file, lands.
    """

    @abstractmethod
    def own_rows(self, profile: Profile, ids: list[int]) -> QuerySet[Any]:
        """The requester's comments among *ids* that carry an image."""

    def get(self, request: HttpRequest) -> JsonResponse:
        """
        Report each named comment image as still processing or settled.

        Args:
            request: The HTTP request, with a comma-separated ``ids`` query param.

        Returns:
            JSON ``{items, processing}`` in the shape ``vault.photos.processing`` answers with. An id in
            neither was deleted, rejected, or is not the requester's.
        """
        ids: list[int] = []
        for raw in (request.GET.get("ids") or "").split(","):
            pk = safe_int_or_none(raw)
            if pk is not None and pk > 0 and pk not in ids:
                ids.append(pk)
        rows = self.own_rows(_profile(request), ids[:_PROCESSING_STATUS_MAX_IDS]) if ids else []
        items = []
        processing = []
        for row in rows:
            if row.pending_scan:
                processing.append(row.pk)
            else:
                items.append({"id": row.pk, "url": row.image.url, "thumb_url": row.image.url, "processing": False, "processing_failed": False})
        return JsonResponse({"items": items, "processing": sorted(processing)})


class PinWikiCommentImageProcessingView(CommentImageProcessingView):
    """GET /comments/images/processing/?ids= for pin notes and wiki comments."""

    def own_rows(self, profile: Profile, ids: list[int]) -> QuerySet[Any]:
        return Comment.objects.filter(profile=profile, pk__in=ids).exclude(image="").exclude(image__isnull=True)


class TripCommentImageProcessingView(CommentImageProcessingView):
    """GET /comments/trip-images/processing/?ids= for trip comments."""

    def own_rows(self, profile: Profile, ids: list[int]) -> QuerySet[Any]:
        from urbanlens.dashboard.models.trips.model import TripComment

        return TripComment.objects.filter(author=profile, pk__in=ids).exclude(image="").exclude(image__isnull=True)


def _render_comments(request, context: dict) -> HttpResponse:
    return render(request, "dashboard/partials/comments/comment_panel.html", context)


def _build_context(comments_qs, profile: Profile, request: HttpRequest, replies_qs=None, projection: ViewerProjection | None = None, **extra) -> dict:
    # Gate before paging so page size stays meaningful.
    visible_qs = comments_qs.visible_to(profile)
    visible_replies = (replies_qs if replies_qs is not None else Comment.objects.all()).visible_to(profile)
    top_level_qs = top_level_comment_queryset(visible_qs, replies_qs=visible_replies)
    # Default to last page to show most recent activity.
    page_obj = get_page(request, top_level_qs, _COMMENTS_PAGE_SIZE, default_last=True)
    top_level = list(page_obj.object_list)

    all_commenters: set[Profile] = set()
    for c in top_level:
        all_commenters.add(c.profile)
        all_commenters.update(r.profile for r in c.replies.all())
    # Batched: per-pair check scans whole Pin table at COMMON_PIN.
    admitted = Profile.visible_photo_uploader_pks(profile, list(all_commenters))
    blurred_profiles: set[int] = {p.pk for p in all_commenters if p.pk != profile.pk and p.pk not in admitted}

    # Shared with the external API so both surfaces stay consistent.
    visible = visible_comment_tree(top_level, profile)

    rendered = [
        {
            "comment": item.comment,
            "rendered_text": item.rendered_text,
            "reactions": _aggregate_reactions(item.comment.reactions.all(), profile, projection=projection),
            "replies": [
                {
                    "comment": reply.comment,
                    "rendered_text": reply.rendered_text,
                    "reactions": _aggregate_reactions(reply.comment.reactions.all(), profile, projection=projection),
                }
                for reply in item.replies
            ],
            "parent_was_deleted": item.parent_was_deleted,
        }
        for item in visible
    ]
    return {
        "rendered_comments": rendered,
        "page_obj": page_obj,
        # Gated count, not raw total, to avoid leaking hidden rows.
        "total_comment_count": visible_comment_count(comments_qs, profile),
        "profile": profile,
        "blurred_profiles": blurred_profiles,
        "allowed_emojis": sorted(_ALLOWED_EMOJIS),
        **extra,
    }


# -- Pin comments --------------------------------------------------------------


def _pin_comments_context(pin, profile: Profile, request: HttpRequest) -> dict:
    """
    Build the Notes panel context for a pin, aggregating child pins' notes on ``?children=1``.

    - a note left on a child pin must not be invisible from the parent's own Notes tab just because it
      happens to live on a nested row. Posting and deleting still ...

    Args:
        pin: The pin whose Notes panel is being rendered.
        profile: The viewing profile (always the pin's owner - notes are private).
        request: The current request, read for the ``children`` flag and pagination.

    Returns:
        The context dict for ``comment_panel.html``.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    include_children = child_details_requested(request, pin)
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
        extra_query=f"children={int(include_children)}",
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
        if not image and (existing_error := existing_image_error(existing_image_id, profile)):
            return HttpResponse(existing_error, status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            # parent__isnull=True: replies render one level deep (visible_comment_tree never walks a reply's own
            # .replies), so a reply-to-a-reply would persist but never appear anywhere.
            parent = get_object_or_404(Comment, id=safe_int_or_none(parent_id), pin=pin, parent__isnull=True)
        try:
            comment = create_comment_with_image(
                lambda: Comment.objects.create(pin=pin, profile=profile, text=text, parent=parent, markup_map=materialize_markup_map(profile, map_data, context=pin)),
                image,
                existing_image_id,
                profile,
            )
        except StorageUnavailableError as exc:
            return HttpResponse(exc.message, status=exc.status, headers=exc.headers)
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
        # A deletable comment may live on any descendant, not just the exact pin in the URL - the aggregated
        # view's delete buttons post back here for a note authored on a child pin.
        subtree = Pin.objects.filter(pk=pin.pk).with_descendants()
        comment = get_object_or_404(Comment, id=comment_id, pin__in=subtree)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        markup_map = comment.markup_map
        comment.delete()
        _discard_comment_image(comment)
        if markup_map is not None:
            # The comment itself is not restorable, but the map attached to it is hand-drawn work - stash it so
            # deleting the comment can't silently destroy the drawing.
            stash_for_undo(MARKUP_MAP_MODEL_LABEL, [markup_map], markup_map.profile)
            markup_map.delete()
        # Replies to a deleted comment survive (parent FK is SET_NULL), becoming orphaned top-level comments.
        ctx = _pin_comments_context(pin, profile, request)
        return _render_comments(request, ctx)


# -- Wiki comments -------------------------------------------------------------


def _visible_wiki_comments(wiki: Wiki, profile: Profile, *, include_children: bool = False) -> QuerySet[Comment]:
    """
    The wiki's comments as *profile* is entitled to see them at page scope.

    One function rather than the same two lines at each of the three call sites: the GET had them and
    the POST and DELETE re-renders did not, so posting a comment handed a viewer the whole
    thread that the page they were looking at had just narrowed.

    Args:
        wiki: The wiki whose comments are being listed.
        profile: The viewer.
        include_children: When True, also list comments on descendant child wikis.

    Returns:
        A comment queryset, narrowed to what this viewer is shown.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

    if include_children:
        subtree = WikiModel.objects.filter(pk=wiki.pk).with_descendants()
        rows = Comment.objects.filter(wiki__in=subtree).select_related("wiki", "wiki__location")
    else:
        rows = wiki.comments.all()
    return visible_rows(rows, wiki, profile)


def _visible_wiki_reply_prefetch(wiki: Wiki, profile: Profile) -> QuerySet[Comment] | None:
    """
    The queryset replies must be prefetched from when the viewer's projection narrows the wiki's comments.

    Narrowing the top level is not enough.

    Args:
        wiki: The wiki whose comments are being listed.
        profile: The viewer.

    Returns:
        A narrowed reply queryset, or None to leave the default prefetch alone.
    """
    projection = viewer_projection(wiki, profile)
    if not projection.active:
        return None
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

    wiki_ids = WikiModel.objects.filter(pk=wiki.pk).with_descendants()
    return projection.rows(Comment.objects.filter(wiki__in=wiki_ids))


def _wiki_comment_addressable_by(wiki: Wiki, profile: Profile, comment_id: int | str) -> Comment:
    """
    Return one wiki comment *profile* may address by id, or 404.

    Addressing a sequential id also needs the per-comment gates (author ``comment_visibility``, pending
    malware scan, ``@loc`` mention) or a guessed id is an existence oracle for comments the listing
    withholds - and a reply would notify an author whose comments this caller is not permitted to read.

    Args:
        wiki: The wiki (or ancestor, when child comments are aggregated) whose thread is being
        addressed.
        profile: The viewer.
        comment_id: The sequential id from the URL or ``parent_id`` field.

    Returns:
        The comment, with ``profile`` selected.

    Raises:
        Http404: Unknown id, or a comment this caller was never shown -
        all indistinguishable.
    """
    comment = get_object_or_404(
        _visible_wiki_comments(wiki, profile, include_children=True).select_related("profile"),
        id=safe_int_or_none(comment_id),
    )
    if not comment_is_visible(comment, profile):
        raise Http404
    return comment


class WikiCommentsView(LoginRequiredMixin, View):
    """GET/POST comment panel for a wiki."""

    def get(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        include_children = child_details_requested(request, wiki)
        ctx = _build_context(
            _visible_wiki_comments(wiki, profile, include_children=include_children),
            profile,
            request,
            replies_qs=_visible_wiki_reply_prefetch(wiki, profile),
            projection=viewer_projection(wiki, profile),
            wiki=wiki,
            location=wiki.location,
            context_type="wiki",
            include_children=include_children,
            extra_query=f"children={int(include_children)}",
        )
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
        if not image and (existing_error := existing_image_error(existing_image_id, profile)):
            return HttpResponse(existing_error, status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = _wiki_comment_addressable_by(wiki, profile, parent_id)
            if parent.parent_id is not None:
                # Replies render one level deep (visible_comment_tree never walks a reply's own .replies) - a
                # reply-to-a-reply would persist but never appear anywhere, so refuse it the same way an
                # unaddressable id already is.
                raise Http404
        try:
            comment = create_comment_with_image(
                lambda: Comment.objects.create(wiki=wiki, profile=profile, text=text, parent=parent, markup_map=materialize_markup_map(profile, map_data, context=wiki)),
                image,
                existing_image_id,
                profile,
            )
        except StorageUnavailableError as exc:
            return HttpResponse(exc.message, status=exc.status, headers=exc.headers)
        if parent and parent.profile != profile:
            notify_reply(profile, parent, reply=comment)
        include_children = child_details_requested(request, wiki)
        ctx = _build_context(
            _visible_wiki_comments(wiki, profile, include_children=include_children),
            profile,
            request,
            replies_qs=_visible_wiki_reply_prefetch(wiki, profile),
            projection=viewer_projection(wiki, profile),
            wiki=wiki,
            location=wiki.location,
            context_type="wiki",
            include_children=include_children,
            extra_query=f"children={int(include_children)}",
        )
        return _render_comments(request, ctx)


class WikiCommentDeleteView(LoginRequiredMixin, View):
    """DELETE /location/<slug>/wiki/comments/<int>/delete/"""

    def delete(self, request, location_slug, comment_id):
        from urbanlens.dashboard.plugins.points import CONTRIBUTION_WITHDRAWN

        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        comment = _wiki_comment_addressable_by(wiki, profile, comment_id)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        markup_map = comment.markup_map
        # Owner-only, per the guard above, so this is always the contributor ending their own contribution - the
        # same case, and the same treatment, as withdrawing a photo from a wiki (see detach_image_from_wiki).
        CONTRIBUTION_WITHDRAWN.fire(comment)
        comment.delete()
        _discard_comment_image(comment)
        if markup_map is not None:
            # The comment itself is not restorable, but the map attached to it is hand-drawn work - stash it so
            # deleting the comment can't silently destroy the drawing.
            stash_for_undo(MARKUP_MAP_MODEL_LABEL, [markup_map], markup_map.profile)
            markup_map.delete()
        # Replies to a deleted comment survive (parent FK is SET_NULL), becoming orphaned top-level comments.
        include_children = child_details_requested(request, wiki)
        ctx = _build_context(
            _visible_wiki_comments(wiki, profile, include_children=include_children),
            profile,
            request,
            replies_qs=_visible_wiki_reply_prefetch(wiki, profile),
            projection=viewer_projection(wiki, profile),
            wiki=wiki,
            location=wiki.location,
            context_type="wiki",
            include_children=include_children,
            extra_query=f"children={int(include_children)}",
        )
        return _render_comments(request, ctx)


# -- Reactions ------------------------------------------------------------------


class CommentReactionView(LoginRequiredMixin, View):
    """POST /comments/<int>/react/  - toggle an emoji reaction on a Comment."""

    def post(self, request, comment_id):
        profile = _profile(request)
        # Only comments on own pins or pinned-location wikis.
        comment = get_object_or_404(
            Comment.objects.filter(Q(pin__profile=profile) | Q(wiki__isnull=False)).select_related("wiki__location", "profile"),
            id=comment_id,
        )
        if comment.wiki is not None:
            if not location_visible_to(comment.wiki.location, profile):
                raise Http404
            # Re-resolve with the viewer's projection applied; the fetch above applies none,
            # so comments they are not shown stay unreachable by guessed id.
            comment = _wiki_comment_addressable_by(comment.wiki, profile, comment_id)
        # Same per-comment gates as listing (visibility, scan, @loc).
        elif not comment_is_visible(comment, profile):
            raise Http404
        try:
            toggle_reaction(profile, comment, request.POST.get("emoji", ""))
        except UnsupportedReactionEmojiError as exc:
            logger.info("comment reaction rejected: %s", exc)
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
            logger.info("trip comment reaction: not found: %s", exc)
            raise Http404("Comment not found.") from exc
        except TripError as exc:
            logger.info("trip comment reaction rejected: %s", exc)
            if isinstance(exc, TripPermissionError):
                return HttpResponse("You don't have permission to react to that comment.", status=403)
            return HttpResponse("Couldn't react to that comment.", status=400)
        return _render_trip_reaction_row(request, comment, profile)


def _render_reaction_row(request, comment: Comment, profile: Profile) -> HttpResponse:
    projection = viewer_projection(comment.wiki, profile) if comment.wiki is not None else None
    reactions = _aggregate_reactions(comment.reactions.all(), profile, projection=projection)
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
    from urbanlens.dashboard.services.trips.trip_comments import comment_reactions_for

    reactions = comment_reactions_for(comment, profile)
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


def _aggregate_reactions(reactions_qs, profile: Profile | None = None, *, projection: ViewerProjection | None = None) -> dict[str, _ReactionData]:
    """
    Group reactions by emoji → {count, reacted_by: list of profile_ids}.

    Args:
        reactions_qs: The comment's reactions.
        profile: The viewer.
        projection: How the viewer reads the wiki the comment belongs to, if it belongs to one - a
            reaction is a contribution like any other, so the projection narrows them too.
    """
    if projection is not None:
        reactions_qs = projection.rows(reactions_qs)
    result: dict[str, _ReactionData] = {}
    # Do not clone the queryset (.all()/select_related): clones drop the
    # prefetch cache and `emoji`/`profile_id` need no join.
    for r in reactions_qs:
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
