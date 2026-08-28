"""Album views - the Photos subpage on a pin or wiki, and album CRUD.

Two parents can own an Album (see :class:`~urbanlens.dashboard.models.album.model.Album`):
a Pin (personal, editable only by its owner) or a Wiki (community, editable by
any signed-in user with wiki access) - the same permission split
``controllers.custom_layers`` uses, resolved here by the same
optional-URL-kwarg pattern so one view class serves both routes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.controllers.image_gallery import create_uploaded_photo
from urbanlens.dashboard.models.album.model import ALBUM_KIND_SPECS, Album, AlbumKind, album_kind_spec
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.celery import safely_enqueue_task
from urbanlens.dashboard.services.core.text_limits import MAX_ALBUM_DESCRIPTION_LENGTH, column_max_length, text_length_error
from urbanlens.dashboard.services.media.images import image_to_gallery_json
from urbanlens.dashboard.services.media.media_relevance import MATERIALIZE_ERROR_MESSAGE
from urbanlens.dashboard.services.photos.albums import (
    ALBUM_GRID_PAGE_SIZE,
    add_images_to_album,
    album_date_range,
    album_date_range_for_ids,
    album_images,
    album_images_page,
    albums_listing,
    cover_from_ids,
    cover_from_images,
    eligible_images_for,
    loose_images_for,
    owner_kwargs,
    remove_images_from_album,
    reorder_album_items,
    visible_album_item_pairs,
)
from urbanlens.dashboard.services.photos.uploads import existing_photo_for_upload
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Read from the column rather than repeated as a literal: the name is
#: truncated to fit, so a widened column would otherwise keep being clipped at
#: the old width with nothing to show why.
_MAX_ALBUM_NAME_LENGTH = column_max_length(Album, "name")


def _resolve_album_owner(request: HttpRequest, pin_slug: str | None, location_slug: str | None) -> tuple[Pin | Wiki, QuerySet[Album]]:
    """Resolve the Album owner (Pin or Wiki) from URL kwargs.

    Same permission split as ``custom_layers._resolve_layer_owner``: pin-scoped
    requires ownership, wiki-scoped goes through ``resolve_visible_wiki`` (any
    signed-in user who has earned access to the wiki may curate its albums,
    matching the shared wiki-editing model).

    Args:
        request: The current HttpRequest (used for the ownership checks).
        pin_slug: Slug of the parent pin, if this is a personal-album route.
        location_slug: Slug of the parent location, if this is a community-album route.

    Returns:
        Tuple of (owner, album queryset already filtered to that owner).

    Raises:
        Http404: Neither slug was supplied.
    """
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, Album.objects.for_pin(pin)
    if location_slug is None:
        raise Http404
    # Filtered by who created it, not hidden outright - an album is a named
    # grouping like a CustomLayer, and the same "your own work back is not a
    # leak" reasoning applies (see custom_layers._resolve_layer_owner). The
    # photos inside a visible album are filtered separately - see
    # services.photos.albums.
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    _location, wiki, profile = resolve_visible_wiki(request, location_slug)
    return wiki, visible_rows(Album.objects.for_wiki(wiki), wiki, profile)


def _get_album(request: HttpRequest, pin_slug: str | None, location_slug: str | None, album_slug: str) -> tuple[Pin | Wiki, QuerySet[Album], Album]:
    """Resolve a single owner-scoped Album, 404ing if it belongs to someone else.

    Args:
        request: The current HttpRequest.
        pin_slug: Slug of the parent pin (personal-album route).
        location_slug: Slug of the parent location (community-album route).
        album_slug: Slug of the album to resolve.

    Returns:
        Tuple of (owner, owner-scoped queryset, the resolved album).
    """
    owner, qs = _resolve_album_owner(request, pin_slug, location_slug)
    return owner, qs, get_object_or_404(qs, slug=album_slug)


def _owner_slug(owner: Pin | Wiki) -> str:
    """Return the slug used in *owner*'s own URL namespace.

    ``slug`` is nullable on the model, but cannot be absent here: both owner
    kinds were fetched *by* this slug in :func:`_resolve_album_owner`, and both
    mint one on save. Declared non-optional rather than propagating an
    ``Optional`` no caller could act on - every use feeds ``reverse()``, which
    turns None into an opaque ``NoReverseMatch`` 500. The guard makes the
    impossible case a clean 404 instead.

    Args:
        owner: The album owner resolved from the URL.

    Returns:
        The owner's URL slug.

    Raises:
        Http404: The owner somehow has no slug.
    """
    slug = owner.slug if isinstance(owner, Pin) else owner.location.slug
    if slug is None:
        raise Http404
    return slug


def _url_prefix(owner: Pin | Wiki) -> str:
    """Return the URL-name prefix for *owner*'s album routes."""
    return "pin.albums" if isinstance(owner, Pin) else "location.wiki.albums"


def _album_row(
    owner: Pin | Wiki,
    album: Album,
    images: list | None = None,
    *,
    cover=None,
    photo_count: int | None = None,
    date_start=None,
    date_end=None,
) -> dict:
    """Build one album's template payload, with its action URLs pre-reversed.

    URLs are built here (by positional ``args``) rather than in-template
    because ``{% url %}`` can't take a dynamic view name plus dynamic kwargs -
    same reasoning as ``custom_layers._render_layer_list``.

    Cover, count, and date range can be passed in (the Photos tab listing
    already computed them without hydrating every photo) or derived from
    *images* when the caller has that list.

    Args:
        owner: The Pin or Wiki the album belongs to.
        album: The album to describe.
        images: The album's viewer-visible photos, in display order, when
            the caller already has them. Omitted on the listing path.
        cover: Precomputed cover photo.
        photo_count: Precomputed visible-photo count.
        date_start: Precomputed earliest capture time.
        date_end: Precomputed latest capture time.

    Returns:
        Dict consumed by ``_album_card.html``/``_album_detail.html``.
    """
    if images is not None:
        if photo_count is None:
            photo_count = len(images)
        if cover is None:
            cover = cover_from_images(album, images)
        if date_start is None and date_end is None:
            date_start, date_end = album_date_range(images)
    prefix = _url_prefix(owner)
    slug = _owner_slug(owner)
    return {
        "album": album,
        "images": images or [],
        "cover": cover,
        "photo_count": photo_count or 0,
        "date_start": date_start,
        "date_end": date_end,
        # The card's own href, so an album tile is a real link (middle-click,
        # copy-link, no-JS) even though HTMX normally handles the click.
        "list_url": reverse(prefix, args=[slug]),
        "detail_url": reverse(f"{prefix}.detail", args=[slug, album.slug]),
        "edit_url": reverse(f"{prefix}.edit", args=[slug, album.slug]),
        "delete_url": reverse(f"{prefix}.delete", args=[slug, album.slug]),
        "add_url": reverse(f"{prefix}.add", args=[slug, album.slug]),
        "remove_url": reverse(f"{prefix}.remove", args=[slug, album.slug]),
        "reorder_url": reverse(f"{prefix}.reorder", args=[slug, album.slug]),
        "upload_url": reverse(f"{prefix}.upload", args=[slug, album.slug]),
        "items_url": reverse(f"{prefix}.items", args=[slug, album.slug]),
    }


def _photo_map_payload(images: list, viewer: Profile | None) -> list[dict]:
    """Describe *images* for the album map layer.

    Only the viewer's own photos are marked movable: repositioning goes through
    the gallery's per-image endpoint, which refuses to move someone else's
    upload. Sending ``movable`` from here keeps the map from offering a drag
    that the server would then reject.

    Args:
        images: The album's viewer-visible photos.
        viewer: The browsing profile.

    Returns:
        One dict per photo that has a position, JSON-serialisable.
    """
    payload = []
    for image in images:
        latitude, longitude = image.effective_latitude, image.effective_longitude
        if latitude is None or longitude is None:
            continue
        payload.append(
            {
                "id": image.pk,
                "url": image.thumb_url,
                "lat": float(latitude),
                "lng": float(longitude),
                "placed": image.latitude is not None and image.longitude is not None,
                "movable": viewer is not None and image.profile_id == viewer.pk,
                "caption": image.caption or "",
            }
        )
    return payload


def _album_detail_context(owner: Pin | Wiki, album: Album, viewer: Profile) -> dict:
    """Assemble the single-album view's context.

    Args:
        owner: The Pin or Wiki the album belongs to.
        album: The album being shown.
        viewer: The browsing profile.

    Returns:
        Template context for ``_album_detail.html``.
    """
    from urbanlens.dashboard.models.images.model import Image

    pairs = visible_album_item_pairs(album, viewer, owner)
    visible_ids = [image_id for _item_id, image_id in pairs]
    page, total = album_images_page(album, viewer, owner, offset=0, limit=ALBUM_GRID_PAGE_SIZE)
    date_start, date_end = album_date_range_for_ids(visible_ids)
    cover = cover_from_ids(album, visible_ids)
    row = _album_row(owner, album, page, cover=cover, photo_count=total, date_start=date_start, date_end=date_end)
    row["grid_images"] = page
    row["available_images"] = list(
        eligible_images_for(owner, viewer).exclude(pk__in=visible_ids).only("id", "uuid", "image", "thumbnail", "caption", "source_url")
    )
    row["back_url"] = reverse(_url_prefix(owner), args=[_owner_slug(owner)])
    row["list_url"] = row["back_url"]
    # The gallery's own per-image endpoint owns repositioning; the album map
    # posts to it rather than growing a second writer for Image coordinates.
    row["reposition_base"] = reverse("pin.gallery" if isinstance(owner, Pin) else "location.wiki.gallery", args=[_owner_slug(owner)])
    map_images = list(Image.objects.filter(pk__in=visible_ids).select_related("location")) if visible_ids else []
    row["map_photos"] = _photo_map_payload(map_images, viewer)
    row["placed_count"] = len(row["map_photos"])
    row["context_type"] = "pin" if isinstance(owner, Pin) else "wiki"
    row["picker_albums"] = _picker_album_payload(owner, viewer, exclude_slug=album.slug)
    _attach_owner_action_urls(row, owner)
    row["album_bulk_actions"] = _album_bulk_actions(inside_album=True)
    row["profile"] = viewer
    row["grid_page_size"] = ALBUM_GRID_PAGE_SIZE
    # Fallback centre for an album whose photos carry no coordinates at all;
    # the map fits to the photos themselves whenever there are any.
    location = owner.location
    row["map_center_lat"] = float(location.latitude) if location is not None and location.latitude is not None else None
    row["map_center_lng"] = float(location.longitude) if location is not None and location.longitude is not None else None
    return row


def _photos_context(owner: Pin | Wiki, viewer: Profile | None) -> dict:
    """Assemble the Photos subpage context: albums first, then loose photos.

    Args:
        owner: The Pin or Wiki whose photos to show.
        viewer: The browsing profile, for the photo-visibility gate.

    Returns:
        Template context for ``_albums_panel.html``.
    """
    is_pin = isinstance(owner, Pin)
    rows = [
        _album_row(
            owner,
            entry.album,
            cover=entry.cover,
            photo_count=entry.photo_count,
            date_start=entry.date_start,
            date_end=entry.date_end,
        )
        for entry in albums_listing(owner, viewer)
    ]
    loose_qs = loose_images_for(owner, viewer)
    loose_count = loose_qs.count()
    ctx = {
        "album_rows": rows,
        "loose_images": list(loose_qs[:ALBUM_GRID_PAGE_SIZE]),
        "loose_count": loose_count,
        "create_url": reverse(_url_prefix(owner), args=[_owner_slug(owner)]),
        "list_url": reverse(_url_prefix(owner), args=[_owner_slug(owner)]),
        "context_type": "pin" if is_pin else "wiki",
        "pin": owner if is_pin else None,
        "wiki": None if is_pin else owner,
        "album_kind_specs": list(ALBUM_KIND_SPECS.values()),
        "picker_albums": [
            {
                "slug": row["album"].slug,
                "name": row["album"].name,
                "photo_count": row["photo_count"],
                "cover_url": row["cover"].thumb_url if row["cover"] else "",
                "add_url": row["add_url"],
            }
            for row in rows
        ],
        "album_bulk_actions": _album_bulk_actions(inside_album=False),
        "profile": viewer,
        "grid_page_size": ALBUM_GRID_PAGE_SIZE,
    }
    _attach_owner_action_urls(ctx, owner)
    return ctx


def _render_photos_panel(request: HttpRequest, owner: Pin | Wiki, viewer: Profile | None) -> HttpResponse:
    """Re-render the whole Photos panel, for HTMX swaps after any mutation."""
    return render(request, "dashboard/partials/albums/_albums_panel.html", _photos_context(owner, viewer))


def _parse_body(request: HttpRequest) -> dict:
    """Parse a JSON request body, tolerating an empty one.

    Returns:
        The decoded object, or an empty dict when the body is empty/invalid.
    """
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}


def _int_ids(raw) -> list[int]:
    """Coerce a JSON list of ids into ints, dropping anything non-numeric."""
    if not isinstance(raw, list):
        return []
    return [int(value) for value in raw if str(value).lstrip("-").isdigit()]


def _page_args(request: HttpRequest) -> tuple[int, int]:
    """Read ``offset``/``limit`` query params for a photo-grid page."""
    try:
        offset = max(0, int(request.GET.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.GET.get("limit") or ALBUM_GRID_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = ALBUM_GRID_PAGE_SIZE
    return offset, min(max(1, limit), 100)


def _photo_tile(image, request: HttpRequest, viewer: Profile) -> dict:
    """One photo's client payload for album grids, lightboxes, and drag/drop."""
    payload = image_to_gallery_json(image, request, viewer)
    payload["item_id"] = getattr(image, "album_item_id", None)
    payload["thumb_url"] = request.build_absolute_uri(image.thumb_url) if image.thumb_url else payload.get("url", "")
    return payload


def _picker_album_payload(owner: Pin | Wiki, viewer: Profile, *, exclude_slug: str | None = None) -> list[dict]:
    """Albums the add/move dialog can target, without dumping every photo URL."""
    rows = []
    prefix = _url_prefix(owner)
    slug = _owner_slug(owner)
    for entry in albums_listing(owner, viewer):
        if exclude_slug and entry.album.slug == exclude_slug:
            continue
        rows.append(
            {
                "slug": entry.album.slug,
                "name": entry.album.name,
                "photo_count": entry.photo_count,
                "cover_url": entry.cover.thumb_url if entry.cover else "",
                "add_url": reverse(f"{prefix}.add", args=[slug, entry.album.slug]),
            }
        )
    return rows


def _attach_owner_action_urls(ctx: dict, owner: Pin | Wiki) -> None:
    """URLs the album UI needs for delete / send-to-wiki / share, when they exist."""
    slug = _owner_slug(owner)
    if isinstance(owner, Pin):
        ctx["gallery_bulk_url"] = reverse("pin.gallery.bulk", args=[slug])
        ctx["pin_share_dialog_url"] = reverse("pin.share.dialog", args=[slug])
    else:
        ctx["gallery_bulk_url"] = ""
        ctx["pin_share_dialog_url"] = ""
    ctx["label_image_url_template"] = reverse("label.image", args=["00000000-0000-0000-0000-000000000000"])


def _album_bulk_actions(*, inside_album: bool) -> list[dict]:
    """Buttons for the shared ``ul-bulk-bar`` on the Photos tab.

    The bar only shows a button when the client supplies a callback for its
    ``action`` key, so list and detail can share this list and hide move/remove
    on the album list by omitting those callbacks.
    """
    actions = [
        {"action": "add_to_album", "icon": "photo_library", "label": "Add to album"},
    ]
    if inside_album:
        actions.extend(
            [
                {"action": "move_to_album", "icon": "drive_file_move", "label": "Move to album"},
                {"action": "remove", "icon": "remove_circle", "label": "Remove from album"},
            ]
        )
    actions.extend(
        [
            {"action": "wiki", "icon": "public", "label": "Send to wiki"},
            {"action": "delete", "icon": "delete", "label": "Delete"},
        ]
    )
    return actions


class AlbumPhotosView(LoginRequiredMixin, View):
    """The Photos subpage body: albums first, then photos not in any album.

    GET /map/pin/<pin_slug>/albums/
    GET /location/<location_slug>/wiki/albums/
    POST (same URLs) creates an album.

    ``?album=<slug>`` renders that album's own view instead of the list, which
    is what makes an opened album a real, shareable URL: the browser's Back
    button and a pasted link both land on the same place. The client pushes
    that query string when an album is opened (see ``shared/album-items.ts``).
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Render the Photos panel, or one album when ``?album=`` is given.

        An ``album`` slug that doesn't resolve falls back to the list rather
        than 404ing - a stale bookmark to a since-deleted album should still
        land somewhere useful.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            The rendered ``_albums_panel.html`` or ``_album_detail.html`` partial.
        """
        owner, qs = _resolve_album_owner(request, pin_slug, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if request.GET.get("picker"):
            albums = _picker_album_payload(owner, profile)
            return JsonResponse({"albums": albums})

        if request.GET.get("loose"):
            offset, limit = _page_args(request)
            qs_images = loose_images_for(owner, profile)
            total = qs_images.count()
            images = list(qs_images[offset : offset + limit])
            return JsonResponse(
                {
                    "items": [_photo_tile(image, request, profile) for image in images],
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                }
            )

        if album_slug := request.GET.get("album"):
            album = qs.filter(slug=album_slug).first()
            if album is not None:
                return render(request, "dashboard/partials/albums/_album_detail.html", _album_detail_context(owner, album, profile))
        return _render_photos_panel(request, owner, profile)

    def post(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Create a new album for this pin or wiki.

        Args:
            request: HttpRequest, with ``name``/``description``/``kind`` fields.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            The re-rendered Photos panel, or a 400 for an invalid name/description.
        """
        owner, _qs = _resolve_album_owner(request, pin_slug, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        name = (request.POST.get("name") or "").strip()[:_MAX_ALBUM_NAME_LENGTH]
        if not name:
            return HttpResponse("Album name is required.", status=400)
        description = (request.POST.get("description") or "").strip()
        if (error := text_length_error(description, MAX_ALBUM_DESCRIPTION_LENGTH, "Description")) is not None:
            return HttpResponse(error, status=400)
        kind = request.POST.get("kind") or AlbumKind.PLAIN
        if kind not in AlbumKind.values:
            kind = AlbumKind.PLAIN

        Album.objects.create(
            name=name,
            description=description,
            kind=kind,
            # A timelapse is a sequence, so it starts manually ordered; a plain
            # grouping doesn't. See AlbumKindSpec.prefers_manual_order.
            manual_order=album_kind_spec(kind).prefers_manual_order,
            profile=profile,
            **owner_kwargs(owner),
        )
        return _render_photos_panel(request, owner, profile)


class AlbumDetailView(LoginRequiredMixin, View):
    """One album's own photo grid.

    GET /map/pin/<pin_slug>/albums/<album_slug>/
    GET /location/<location_slug>/wiki/albums/<album_slug>/
    """

    def get(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Render one album's contents.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to show.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            The rendered ``_album_detail.html`` partial.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(request, "dashboard/partials/albums/_album_detail.html", _album_detail_context(owner, album, profile))


class AlbumEditView(LoginRequiredMixin, View):
    """Rename an album / change its blurb, kind, or manual-order flag.

    POST /map/pin/<pin_slug>/albums/<album_slug>/edit/
    POST /location/<location_slug>/wiki/albums/<album_slug>/edit/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Apply an album edit.

        Only fields actually present in the POST are touched, so a partial
        form (e.g. just the manual-order toggle) can't blank out the rest.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to edit.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            The re-rendered Photos panel, or a 400 for invalid input.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        fields: list[str] = []
        if "name" in request.POST:
            name = (request.POST.get("name") or "").strip()[:_MAX_ALBUM_NAME_LENGTH]
            if not name:
                return HttpResponse("Album name is required.", status=400)
            album.name = name
            fields.append("name")
        if "description" in request.POST:
            description = (request.POST.get("description") or "").strip()
            if (error := text_length_error(description, MAX_ALBUM_DESCRIPTION_LENGTH, "Description")) is not None:
                return HttpResponse(error, status=400)
            album.description = description
            fields.append("description")
        if "kind" in request.POST:
            kind = request.POST.get("kind") or AlbumKind.PLAIN
            if kind in AlbumKind.values:
                album.kind = kind
                fields.append("kind")
        if "manual_order" in request.POST:
            album.manual_order = request.POST.get("manual_order") in ("1", "true", "on", "True")
            fields.append("manual_order")
        if "cover_image_id" in request.POST:
            raw = request.POST.get("cover_image_id") or ""
            # Re-scope through the album's own contents so a foreign image id
            # can't be pinned as a cover.
            allowed = {image.pk for image in album_images(album, profile, owner=owner)}
            album.cover_image_id = int(raw) if raw.isdigit() and int(raw) in allowed else None
            fields.append("cover_image")

        if fields:
            album.save(update_fields=[*fields, "updated"])
        return _render_photos_panel(request, owner, profile)


class AlbumDeleteView(LoginRequiredMixin, View):
    """Delete an album. Its photos survive and fall back to the loose section.

    POST /map/pin/<pin_slug>/albums/<album_slug>/delete/
    POST /location/<location_slug>/wiki/albums/<album_slug>/delete/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Delete the album.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to delete.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            The re-rendered Photos panel.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        album.delete()
        return _render_photos_panel(request, owner, profile)


class AlbumAddPhotosView(LoginRequiredMixin, View):
    """Add photos - already-local ones, or an external gallery item - to an album.

    POST /map/pin/<pin_slug>/albums/<album_slug>/add/
    POST /location/<location_slug>/wiki/albums/<album_slug>/add/

    Body is JSON. ``image_ids`` adds existing local photos. ``media`` adds an
    external Media-gallery item, which additionally counts as a "relevant"
    vote and caches a local copy - unless the caller already voted that item
    *not* relevant, in which case their vote stands and nothing is added.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Add photos to the album.

        Args:
            request: HttpRequest with a JSON body.
            album_slug: Slug of the album to add to.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            JSON with how many photos were added, plus ``declined``/``error``
            when an external item was skipped or failed to download.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        body = _parse_body(request)

        response: dict = {"added": 0}

        image_ids = _int_ids(body.get("image_ids"))
        if image_ids:
            # Re-scope through eligible_images_for so an id from another place
            # (or one this viewer can't see) can't be filed into this album.
            images = list(eligible_images_for(owner, profile).filter(pk__in=image_ids))
            response["added"] += add_images_to_album(album, images, profile)
            move_from = (body.get("move_from") or "").strip()
            if move_from and move_from != album.slug:
                source = _qs.filter(slug=move_from).first()
                if source is not None:
                    response["removed"] = remove_images_from_album(source, image_ids)

        media = body.get("media")
        if isinstance(media, dict) and media.get("url"):
            result = self._add_external(owner, album, profile, media)
            response.update(result)

        return JsonResponse(response)

    def _add_external(self, owner: Pin | Wiki, album: Album, profile: Profile, media: dict) -> dict:
        """Vote an external gallery item relevant, then cache and file it.

        The vote is written inline because it's a cheap DB write and it's the
        part that must not be lost. The download is handed to a Celery worker,
        since it blocks on a remote server for up to 15s and the user is
        waiting on this response. If the broker is unreachable the download
        falls back to running inline rather than silently never happening.

        Args:
            owner: The Pin or Wiki that owns the album.
            album: The album to add to.
            profile: The acting profile.
            media: The gallery tile's ``source``/``url``/``page_url``/``caption``.

        Returns:
            Partial response dict describing the outcome.
        """
        from urbanlens.dashboard.services.media.media_relevance import VotePolicy, record_relevant_and_cache
        from urbanlens.dashboard.tasks import cache_media_item_into_album

        is_pin = isinstance(owner, Pin)
        location = owner.location
        if location is None:
            return {"error": "This place has no location to attach media to."}

        source = str(media.get("source") or "")[:30]
        url = str(media["url"])
        page_url = str(media.get("page_url") or "")
        caption = str(media.get("caption") or "")

        # Record the vote without downloading, so an already-down-voted item is
        # rejected before any work is queued.
        vote = record_relevant_and_cache(
            location=location,
            profile=profile,
            source=source,
            url=url,
            page_url=page_url,
            caption=caption,
            pin=owner if is_pin else None,
            wiki=None if is_pin else owner,
            policy=VotePolicy.IMPLIED,
            materialize=False,
        )
        if vote.declined:
            return {"declined": True, "message": "You already marked this photo as not relevant."}
        if vote.error:
            return {"error": vote.error}

        queued = safely_enqueue_task(cache_media_item_into_album, album.pk, profile.pk, source, url, page_url=page_url, caption=caption)
        if queued is not None:
            return {"queued": True, "message": "Saving this photo - it'll appear in the album shortly."}

        # Broker unreachable: do it inline so the add still completes.
        logger.warning("AlbumAddPhotosView: broker unavailable, materializing %s inline", url)
        result = cache_media_item_into_album(album.pk, profile.pk, source, url, page_url=page_url, caption=caption)
        if result is None:
            return {"error": MATERIALIZE_ERROR_MESSAGE}
        return {"added": 1, "image_id": result}


class AlbumUploadView(LoginRequiredMixin, View):
    """Upload a photo straight into an album.

    POST /map/pin/<pin_slug>/albums/<album_slug>/upload/
    POST /location/<location_slug>/wiki/albums/<album_slug>/upload/

    Multipart, one ``image`` file per request - same contract as the pin and
    wiki galleries, whose response body the client reuses to render the new
    tile. The photo is created against the album's owner first, so it lands in
    the owner's gallery too; filing it in the album is the extra step.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Store the uploaded photo and file it in this album.

        Args:
            request: HttpRequest carrying the multipart ``image`` file.
            album_slug: Slug of the album to upload into.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            201 with the gallery JSON for the new photo, or the rejection's
            own status (400/409/413/415) with an ``error`` message.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        image, response = create_uploaded_photo(request, owner, profile)
        if image is not None:
            add_images_to_album(album, [image], profile)
            return response
        if response.status_code == 409:
            existing = existing_photo_for_upload(owner, profile, request.FILES.get("image"))
            if existing is not None:
                add_images_to_album(album, [existing], profile)
                return JsonResponse(image_to_gallery_json(existing, request, profile))
        return response


class AlbumRemovePhotosView(LoginRequiredMixin, View):
    """Remove photos from an album without deleting the photos themselves.

    POST /map/pin/<pin_slug>/albums/<album_slug>/remove/
    POST /location/<location_slug>/wiki/albums/<album_slug>/remove/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Remove photos from the album.

        Args:
            request: HttpRequest with a JSON ``image_ids`` list.
            album_slug: Slug of the album to remove from.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            JSON with how many membership rows were removed.
        """
        _owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        image_ids = _int_ids(_parse_body(request).get("image_ids"))
        return JsonResponse({"removed": remove_images_from_album(album, image_ids)})


class AlbumItemsView(LoginRequiredMixin, View):
    """Paginated JSON of one album's photos, for the virtualized grid.

    GET /map/pin/<pin_slug>/albums/<album_slug>/items/
    GET /location/<location_slug>/wiki/albums/<album_slug>/items/
    """

    def get(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Return one page of this album's viewer-visible photos.

        Args:
            request: HttpRequest with ``offset``/``limit`` query params.
            album_slug: Slug of the album to page.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        offset, limit = _page_args(request)
        images, total = album_images_page(album, profile, owner, offset=offset, limit=limit)
        return JsonResponse(
            {
                "items": [_photo_tile(image, request, profile) for image in images],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )


class AlbumReorderView(LoginRequiredMixin, View):
    """Persist a drag-and-drop reordering of an album's photos.

    POST /map/pin/<pin_slug>/albums/<album_slug>/reorder/
    POST /location/<location_slug>/wiki/albums/<album_slug>/reorder/

    Body: ``{"items": [<AlbumItem id>, ...]}`` in the new display order.
    Reordering implies the album is manually ordered, so this also flips
    ``manual_order`` on - otherwise the new order would be saved and then
    ignored at render time.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Apply the new item order.

        Args:
            request: HttpRequest with a JSON ``items`` list of AlbumItem ids.
            album_slug: Slug of the album being reordered.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).

        Returns:
            JSON with how many items were renumbered.
        """
        _owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        item_ids = _int_ids(_parse_body(request).get("items"))
        reordered = reorder_album_items(album, item_ids)
        if reordered and not album.manual_order:
            album.manual_order = True
            album.save(update_fields=["manual_order", "updated"])
        return JsonResponse({"reordered": reordered, "manual_order": album.manual_order})
