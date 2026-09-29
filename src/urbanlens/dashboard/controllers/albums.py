"""Album views - Photos subpage on a pin or wiki, and album CRUD."""

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
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.capacity import ALBUM_PHOTOS, CapacityExceededError, ensure_room
from urbanlens.dashboard.services.core.celery import safely_enqueue_task
from urbanlens.dashboard.services.core.text_limits import MAX_ALBUM_DESCRIPTION_LENGTH, column_max_length, text_length_error
from urbanlens.dashboard.services.geo.sampling import bound_map_layer
from urbanlens.dashboard.services.media.images import image_to_gallery_json
from urbanlens.dashboard.services.media.media_relevance import MATERIALIZE_ERROR_MESSAGE
from urbanlens.dashboard.services.photos.albums import (
    ALBUM_GRID_PAGE_SIZE,
    AlbumListEntry,
    add_images_to_album,
    album_images_page,
    albums_listing_page,
    describe_album,
    eligible_images_for,
    filed_image_ids,
    loose_images_for,
    move_album_targets,
    move_album_to_pin,
    owner_images_for,
    owner_kwargs,
    pin_tree,
    remove_images_from_album,
    reorder_album_items,
    visible_album_items,
)
from urbanlens.dashboard.services.photos.pin_photos import MAX_PAGE_SIZE, external_photos_for_pin
from urbanlens.dashboard.services.photos.uploads import existing_photo_for_upload
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Read from the column rather than repeated as a literal: the name is truncated to fit, so a widened column
#: would otherwise keep being clipped at the old width with nothing to show why.
_MAX_ALBUM_NAME_LENGTH = column_max_length(Album, "name")


def _panel_owner(request: HttpRequest, owner: Pin | Wiki | Profile) -> Pin | Wiki | Profile:
    """The pin whose Photos tab we should re-render after a mutation.

    Album action URLs use the album's own pin slug.
    """
    slug = request.GET.get("from_pin") or request.POST.get("from_pin")
    if slug and isinstance(owner, Pin) and slug != owner.slug:
        viewing = Pin.objects.filter(slug=slug, profile_id=owner.profile_id).first()
        if viewing is not None and Pin.objects.filter(pk=viewing.pk).with_descendants().filter(pk=owner.pk).exists():
            return viewing
    return owner


def _include_children(request: HttpRequest, owner: Pin | Wiki | Profile) -> bool:
    """Whether this request should list descendant pins'/wikis' albums and photos."""
    flag = request.GET.get("children") or request.POST.get("children")
    if flag == "1" and isinstance(owner, (Pin, Wiki)):
        return True
    return bool(request.GET.get("from_pin") or request.POST.get("from_pin")) and isinstance(owner, Pin)


def _listing_owners(owner: Pin | Wiki | Profile, include_children: bool) -> list[Pin | Wiki | Profile]:
    """The pin/wiki/vault set whose albums appear on this Photos tab."""
    if include_children and isinstance(owner, Pin):
        return list(Pin.objects.filter(pk=owner.pk).with_descendants().select_related("location"))
    if include_children and isinstance(owner, Wiki):
        return list(Wiki.objects.filter(pk=owner.pk).with_descendants().select_related("location"))
    return [owner]


def _safe_back_url(raw: str | None) -> str | None:
    """Return *raw* if it is a same-origin path, else None."""
    from urllib.parse import urlparse

    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    return raw


def _children_query(include_children: bool) -> str:
    return "?children=1" if include_children else ""


def _query_url(base: str, **params: str | int) -> str:
    """*base* with *params* appended to whatever query string it already carries."""
    from urllib.parse import urlencode

    return f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"


#: Re-render cadence of the public-source section while a provider is still fetching; each one resets its grid.
_EXTERNAL_POLL_SECONDS = 5

#: ``?photos=`` values for the pin Photos tab's "Your photos" grid.
_PHOTOS_FILTER_ALL = "all"
_PHOTOS_FILTER_LOOSE = "loose"


def _resolve_album_owner(request: HttpRequest, pin_slug: str | None, location_slug: str | None, *, vault: bool = False) -> tuple[Pin | Wiki | Profile, QuerySet[Album]]:
    """Resolve the Album owner (Pin, Wiki, or the requesting profile's Vault) from URL kwargs.

    A Vault route carries no owner slug at all - there is exactly one Vault per profile, resolved from
    the request itself rather than a URL segment.

    Args:
        request: The current HttpRequest (used for the ownership checks).
        pin_slug: Slug of the parent pin, if this is a personal-album route.
        location_slug: Slug of the parent location, if this is a community-album route.
        vault: True for a Vault (Profile-owned) album route; *pin_slug* and *location_slug* are ignored
        when set.

    Returns:
        Tuple of (owner, album queryset already filtered to that owner).

    Raises:
        Http404: Neither a slug nor ``vault`` was supplied.
    """
    if vault:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return profile, Album.objects.for_profile(profile)
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, Album.objects.for_pin(pin)
    if location_slug is None:
        raise Http404
    # Filtered by who created it, not hidden outright - an album is a named grouping like a CustomLayer, and the
    # same "your own work back is not a leak" reasoning applies (see custom_layers._resolve_layer_owner).
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    _location, wiki, profile = resolve_visible_wiki(request, location_slug)
    return wiki, visible_rows(Album.objects.for_wiki(wiki), wiki, profile)


def _get_album(request: HttpRequest, pin_slug: str | None, location_slug: str | None, album_slug: str, *, vault: bool = False) -> tuple[Pin | Wiki | Profile, QuerySet[Album], Album]:
    """Resolve a single owner-scoped Album, 404ing if it belongs to someone else.

    Args:
        request: The current HttpRequest.
        pin_slug: Slug of the parent pin (personal-album route).
        location_slug: Slug of the parent location (community-album route).
        album_slug: Slug of the album to resolve.
        vault: True for a Vault (Profile-owned) album route.

    Returns:
        Tuple of (owner, owner-scoped queryset, the resolved album).
    """
    owner, qs = _resolve_album_owner(request, pin_slug, location_slug, vault=vault)
    return owner, qs, get_object_or_404(qs, slug=album_slug)


def _owner_slug(owner: Pin | Wiki) -> str:
    """Return the slug used in *owner*'s own URL namespace.

    ``slug`` is nullable on the model, but cannot be absent here: both owner kinds were fetched *by*
    this slug in :func:`_resolve_album_owner`, and both mint one on save.

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


def _url_prefix(owner: Pin | Wiki | Profile) -> str:
    """Return the URL-name prefix for *owner*'s album routes."""
    if isinstance(owner, Pin):
        return "pin.albums"
    if isinstance(owner, Profile):
        return "vault.photos.albums"
    return "location.wiki.albums"


def _owner_url_args(owner: Pin | Wiki | Profile) -> list[str]:
    """Return the positional ``reverse()`` args identifying *owner* in its own URL namespace.

    A vault (Profile-owned) album has no owner-slug segment - there is one implicit album space per
    user, resolved from the request - so this is empty for a Profile and ``[_owner_slug(owner)]`` for a
    Pin/Wiki.
    """
    if isinstance(owner, Profile):
        return []
    return [_owner_slug(owner)]


def _album_row(
    owner: Pin | Wiki | Profile,
    album: Album,
    images: list | None = None,
    *,
    cover=None,
    photo_count: int | None = None,
    date_start=None,
    date_end=None,
) -> dict:
    """Build one album's template payload, with its action URLs pre-reversed.

    URLs are built here (by positional ``args``) rather than in-template because ``{% url %}`` can't
    take a dynamic view name plus dynamic kwargs - same reasoning as
    ``custom_layers._render_layer_list``.

    Args:
        owner: The Pin, Wiki, or Profile the album belongs to.
        album: The album to describe.
        images: The page of the album's photos to render, when the caller is rendering one.
        cover: Precomputed cover photo.
        photo_count: Precomputed visible-photo count.
        date_start: Precomputed earliest capture time.
        date_end: Precomputed latest capture time.

    Returns:
        Dict consumed by ``_album_card.html``/``_album_detail.html``.
    """
    prefix = _url_prefix(owner)
    owner_args = _owner_url_args(owner)
    return {
        "album": album,
        "images": images or [],
        "cover": cover,
        "photo_count": photo_count or 0,
        "date_start": date_start,
        "date_end": date_end,
        # The card's own href, so an album tile is a real link (middle-click,
        # copy-link, no-JS) even though HTMX normally handles the click.
        "list_url": reverse(prefix, args=owner_args),
        "detail_url": reverse(f"{prefix}.detail", args=[*owner_args, album.slug]),
        "edit_url": reverse(f"{prefix}.edit", args=[*owner_args, album.slug]),
        "delete_url": reverse(f"{prefix}.delete", args=[*owner_args, album.slug]),
        "add_url": reverse(f"{prefix}.add", args=[*owner_args, album.slug]),
        "remove_url": reverse(f"{prefix}.remove", args=[*owner_args, album.slug]),
        "reorder_url": reverse(f"{prefix}.reorder", args=[*owner_args, album.slug]),
        "upload_url": reverse(f"{prefix}.upload", args=[*owner_args, album.slug]),
        "items_url": reverse(f"{prefix}.items", args=[*owner_args, album.slug]),
        "move_url": reverse(f"{prefix}.move", args=[*owner_args, album.slug]) if isinstance(owner, Pin) else "",
        "owner_pin_name": "",
        "detail_query": "",
    }


def _photo_map_payload(images: list, viewer: Profile | None) -> list[dict]:
    """Describe *images* for the album map layer.

    Sending ``movable`` from here keeps the map from offering a drag that the server would then reject.

    Args:
        images: The album's viewer-visible photos.
        viewer: The browsing profile.

    Returns:
        One dict per photo that has a position, JSON-serialisable.
    """
    payload = []
    for image in images:
        latitude, longitude = image.effective_latitude, image.effective_longitude
        if latitude is None or longitude is None or getattr(image, "map_hidden", False) or image.pending_scan:
            continue
        payload.append(
            {
                "id": image.pk,
                "url": image.marker_thumb_url,
                "lat": float(latitude),
                "lng": float(longitude),
                "placed": image.latitude is not None and image.longitude is not None,
                "movable": viewer is not None and image.profile_id == viewer.pk,
                "caption": image.caption or "",
            }
        )
    return payload


def _album_detail_context(owner: Pin | Wiki | Profile, album: Album, viewer: Profile) -> dict:
    """Assemble the single-album view's context.

    Args:
        owner: The Pin, Wiki, or Profile the album belongs to.
        album: The album being shown.
        viewer: The browsing profile.

    Returns:
        Template context for ``_album_detail.html``.
    """
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.images.queryset import prime_viewer_scope

    # Visibility is resolved several times below, and each resolution costs the viewer's friends, pinned
    # locations, trip memberships and reachable wikis.
    prime_viewer_scope(viewer)

    visible_image_ids = visible_album_items(album, viewer, owner).values("image_id")
    page, _total = album_images_page(album, viewer, owner, offset=0, limit=ALBUM_GRID_PAGE_SIZE)
    entry = describe_album(album, viewer, owner)
    row = _album_row(owner, album, page, cover=entry.cover, photo_count=entry.photo_count, date_start=entry.date_start, date_end=entry.date_end)
    row["grid_images"] = page
    row["available_image_count"] = eligible_images_for(owner, viewer).exclude(pk__in=visible_image_ids).count()
    row["eligible_url"] = reverse(f"{_url_prefix(owner)}.eligible", args=[*_owner_url_args(owner), album.slug])
    row["back_url"] = reverse(_url_prefix(owner), args=_owner_url_args(owner))
    row["list_url"] = row["back_url"]
    # The gallery's own per-image endpoint owns repositioning; the album map posts to it rather than growing a
    # second writer for Image coordinates.
    row["reposition_base"] = "" if isinstance(owner, Profile) else reverse("pin.gallery" if isinstance(owner, Pin) else "location.wiki.gallery", args=_owner_url_args(owner))
    # The grid pages at ALBUM_GRID_PAGE_SIZE; the map used to embed every photo
    # in the album inline in the same response, so pagination bounded what you
    # could see and nothing bounded what was sent. Capped by the same mechanism
    # the gallery map layers use, which keeps the area covered rather than
    # keeping the first N - see services/geo/sampling.py.
    map_queryset, map_truncated, map_total = bound_map_layer(Image.objects.filter(pk__in=visible_image_ids, latitude__isnull=False, longitude__isnull=False))
    map_images = list(map_queryset.select_related("location"))
    row["map_photos"] = _photo_map_payload(map_images, viewer)
    row["placed_count"] = map_total
    row["map_truncated"] = map_truncated
    row["map_total"] = map_total
    row["context_type"] = "pin" if isinstance(owner, Pin) else "wiki" if isinstance(owner, Wiki) else "vault"
    row["picker_url"] = _query_url(_list_url(owner, include_children=False), picker=1, exclude=album.slug or "")
    _attach_owner_action_urls(row, owner)
    row["album_bulk_actions"] = _album_bulk_actions(inside_album=True)
    row["profile"] = viewer
    row["grid_page_size"] = ALBUM_GRID_PAGE_SIZE
    row["move_targets"] = [{"slug": pin.slug, "name": pin.effective_name} for pin in move_album_targets(album)] if isinstance(owner, Pin) else []
    row["failure_url"] = reverse("vault.photos.failures")
    row["pin"] = owner if isinstance(owner, Pin) else None
    # Fallback centre for an album whose photos carry no coordinates at all; the map fits to the photos
    # themselves whenever there are any. A vault album has no single place to fall back to.
    location = owner.location if isinstance(owner, (Pin, Wiki)) else None
    row["map_center_lat"] = float(location.latitude) if location is not None and location.latitude is not None else None
    row["map_center_lng"] = float(location.longitude) if location is not None and location.longitude is not None else None
    return row


def _photos_context(owner: Pin | Wiki | Profile, viewer: Profile | None, *, include_children: bool = False, photos_filter: str = _PHOTOS_FILTER_ALL) -> dict:
    """Assemble the Photos subpage context: albums first, then photos.

    A pin lists every one of its photos, filed or not, under "Your photos" (``photos_filter`` narrows that
    to the unfiled ones), and loads its public-source photos as a section of their own. A wiki lists only
    the photos not in an album.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) whose photos to show.
        viewer: The browsing profile, for the photo-visibility gate.
        include_children: When True on a pin, also list descendant albums and unfiled photos.
        photos_filter: ``"all"`` or ``"loose"``, for a pin's "Your photos" grid.

    Returns:
        Template context for ``_albums_panel.html``.
    """
    is_pin = isinstance(owner, Pin)
    listing = _listing_owners(owner, include_children)
    list_url = _list_url(owner, include_children)
    entries, album_count = albums_listing_page(listing, viewer, limit=ALBUM_GRID_PAGE_SIZE)
    rows = _album_list_rows(owner, listing, entries, include_children=include_children, list_url=list_url)
    loose_qs = loose_images_for(listing, viewer)
    loose_count = loose_qs.count()
    is_wiki = isinstance(owner, Wiki)
    loose_items_url = _query_url(list_url, loose=1)
    grid_qs, grid_count, grid_items_url = loose_qs, loose_count, loose_items_url
    own_count = loose_count
    if is_pin:
        own_qs = owner_images_for(listing, viewer)
        own_count = own_qs.count()
        if photos_filter != _PHOTOS_FILTER_LOOSE:
            photos_filter = _PHOTOS_FILTER_ALL
            grid_qs, grid_count, grid_items_url = own_qs, own_count, _query_url(list_url, mine=1)
    ctx = {
        "album_rows": rows,
        "album_count": album_count,
        "album_items_url": _query_url(list_url, albums=1),
        "picker_url": _query_url(list_url, picker=1),
        "loose_images": list(grid_qs[:ALBUM_GRID_PAGE_SIZE]),
        "loose_count": loose_count,
        "grid_count": grid_count,
        "grid_items_url": grid_items_url,
        "loose_items_url": loose_items_url,
        "own_count": own_count,
        "photos_filter": photos_filter,
        "photos_filter_urls": {
            _PHOTOS_FILTER_ALL: _query_url(list_url, photos=_PHOTOS_FILTER_ALL),
            _PHOTOS_FILTER_LOOSE: _query_url(list_url, photos=_PHOTOS_FILTER_LOOSE),
        },
        "refresh_url": _query_url(list_url, photos=photos_filter) if is_pin else reverse(_url_prefix(owner), args=_owner_url_args(owner)),
        "external_section_url": _query_url(list_url, external_section=1) if is_pin else "",
        "create_url": reverse(_url_prefix(owner), args=_owner_url_args(owner)),
        "list_url": list_url,
        "context_type": "pin" if is_pin else "wiki" if is_wiki else "vault",
        "pin": owner if is_pin else None,
        "wiki": owner if is_wiki else None,
        "album_kind_specs": list(ALBUM_KIND_SPECS.values()),
        "album_bulk_actions": _album_bulk_actions(inside_album=False),
        "profile": viewer,
        "grid_page_size": ALBUM_GRID_PAGE_SIZE,
        "include_children": include_children,
        "failure_url": reverse("vault.photos.failures"),
        "move_targets": [{"slug": pin.slug, "name": pin.effective_name} for pin in pin_tree(owner)] if isinstance(owner, Pin) else [],
    }
    _attach_owner_action_urls(ctx, owner)
    return ctx


def _render_photos_panel(request: HttpRequest, owner: Pin | Wiki | Profile, viewer: Profile | None) -> HttpResponse:
    """Re-render the whole Photos panel, for HTMX swaps after any mutation."""
    viewing = _panel_owner(request, owner)
    photos_filter = request.GET.get("photos") or request.POST.get("photos") or _PHOTOS_FILTER_ALL
    return render(
        request,
        "dashboard/partials/albums/_albums_panel.html",
        _photos_context(viewing, viewer, include_children=_include_children(request, viewing), photos_filter=photos_filter),
    )


def _mine_page(request: HttpRequest, listing: list[Pin | Wiki | Profile], profile: Profile) -> JsonResponse:
    """One page of a pin's own photos, filed or not, each saying whether it is in an album."""
    offset, limit = _page_args(request)
    qs = owner_images_for(listing, profile)
    total = qs.count()
    images = list(qs[offset : offset + limit])
    filed = filed_image_ids(listing, [image.pk for image in images])
    items = []
    for image in images:
        payload = _photo_tile(image, request, profile)
        payload["origin"] = "own"
        payload["in_album"] = image.pk in filed
        payload["media_key"] = image.media_item_key or ""
        items.append(payload)
    return JsonResponse({"items": items, "total": total, "offset": offset, "limit": limit})


def _external_response(request: HttpRequest, pin: Pin, listing: list[Pin | Wiki | Profile], profile: Profile, include_children: bool) -> HttpResponse:
    """A pin's public-source photos: one JSON page (``?external=1``), or the Photos tab section (``?external_section=1``)."""
    from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS

    external = external_photos_for_pin(pin, profile, request.user, own_pins=[entry for entry in listing if isinstance(entry, Pin)])
    total = len(external.photos)
    if request.GET.get("external"):
        offset, limit = _page_args(request)
        return JsonResponse(
            {
                "items": [photo.to_json() for photo in external.photos[offset : offset + limit]],
                "total": total,
                "offset": offset,
                "limit": limit,
                "pending": external.pending,
            },
        )
    list_url = reverse("pin.albums", args=[_owner_slug(pin)]) + _children_query(include_children)
    try:
        attempt = max(int(request.GET.get("attempt") or 0), 0)
    except ValueError:
        attempt = 0
    polling = bool(external.pending) and attempt < MAX_POLL_ATTEMPTS
    return render(
        request,
        "dashboard/partials/albums/_external_photos_section.html",
        {
            "external_count": total,
            "external_items_url": _query_url(list_url, external=1),
            "pending_count": len(external.pending) if polling else 0,
            "poll_url": _query_url(list_url, external_section=1, attempt=attempt + 1) if polling else "",
            "poll_interval": _EXTERNAL_POLL_SECONDS,
            "grid_page_size": ALBUM_GRID_PAGE_SIZE,
        },
    )


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
    return offset, min(max(1, limit), MAX_PAGE_SIZE)


def _photo_tile(image, request: HttpRequest, viewer: Profile) -> dict:
    """One photo's client payload for album grids, lightboxes, and drag/drop."""
    payload = image_to_gallery_json(image, request, viewer)
    payload["item_id"] = getattr(image, "album_item_id", None)
    if payload["url"] and image.thumb_url:
        payload["thumb_url"] = request.build_absolute_uri(image.thumb_url)
    elif not payload["thumb_url"]:
        payload["thumb_url"] = payload["url"]
    return payload


def _list_url(owner: Pin | Wiki | Profile, include_children: bool) -> str:
    """The Photos tab URL for *owner*, which its grid and picker pages hang their query strings on."""
    return reverse(_url_prefix(owner), args=_owner_url_args(owner)) + _children_query(include_children)


def _listed_album_owner(album: Album, listing: list[Pin | Wiki | Profile], fallback: Pin | Wiki | Profile) -> Pin | Wiki | Profile:
    """*album*'s owner, found among *listing* for a wiki album rather than by a query per album."""
    if album.parent_pin is not None:
        return album.parent_pin
    if album.parent_wiki_id is not None:
        wiki = next((entry for entry in listing if isinstance(entry, Wiki) and entry.pk == album.parent_wiki_id), None)
        return wiki or album.parent_wiki or fallback
    return fallback


def _album_list_rows(
    owner: Pin | Wiki | Profile,
    listing: list[Pin | Wiki | Profile],
    entries: list[AlbumListEntry],
    *,
    include_children: bool,
    list_url: str,
) -> list[dict]:
    """Album-card payloads for one page of the Photos tab's album grid.

    Args:
        owner: The Pin, Wiki, or Profile whose Photos tab this is.
        listing: The owners whose albums the tab lists (*owner*, plus descendants when included).
        entries: The page of albums to describe.
        include_children: Whether descendant albums are listed, which labels them with their own pin or wiki.
        list_url: The tab's own URL, which a child album's view returns to.

    Returns:
        One ``_album_card.html`` row per entry.
    """
    from urllib.parse import urlencode

    rows = []
    for entry in entries:
        album_owner = _listed_album_owner(entry.album, listing, owner)
        row = _album_row(
            album_owner,
            entry.album,
            cover=entry.cover,
            photo_count=entry.photo_count,
            date_start=entry.date_start,
            date_end=entry.date_end,
        )
        if include_children and isinstance(album_owner, Pin) and isinstance(owner, Pin) and album_owner.pk != owner.pk:
            row["owner_pin_name"] = album_owner.effective_name
            row["detail_query"] = urlencode({"back": list_url, "from_pin": _owner_slug(owner), "children": "1"})
        elif include_children and isinstance(album_owner, Wiki) and album_owner.pk != owner.pk:
            row["owner_pin_name"] = album_owner.name
            row["detail_query"] = urlencode({"back": list_url, "children": "1"})
        rows.append(row)
    return rows


def _album_grid_page(request: HttpRequest, owner: Pin | Wiki | Profile, listing: list[Pin | Wiki | Profile], viewer: Profile, include_children: bool) -> JsonResponse:
    """One page of the album grid, each card rendered by the template the first page uses."""
    from django.template.loader import get_template

    offset, limit = _page_args(request)
    entries, total = albums_listing_page(listing, viewer, offset=offset, limit=limit)
    card = get_template("dashboard/partials/albums/_album_card.html")
    rows = _album_list_rows(owner, listing, entries, include_children=include_children, list_url=_list_url(owner, include_children))
    items = [{"slug": row["album"].slug, "html": card.render({"row": row})} for row in rows]
    return JsonResponse({"items": items, "total": total, "offset": offset, "limit": limit})


def _album_picker_rows(
    request: HttpRequest,
    owner: Pin | Wiki | Profile,
    owner_albums: QuerySet[Album],
    listing: list[Pin | Wiki | Profile],
    viewer: Profile,
    include_children: bool,
) -> HttpResponse:
    """One page of the add/move dialog's albums, narrowed by ``?q=``, as ``<li>`` rows.

    Args:
        request: HttpRequest with optional ``q``, ``exclude`` (one of *owner*'s album slugs), and ``offset``.
        owner: The Pin, Wiki, or Profile whose albums the dialog targets.
        owner_albums: *owner*'s albums as this viewer may address them, to resolve ``exclude`` against.
        listing: The owners whose albums are offered.
        viewer: The browsing profile.
        include_children: Whether descendant albums are offered.

    Returns:
        The rendered ``_album_picker_rows.html``, ending in a row that loads the next page when there is one.
    """
    offset, limit = _page_args(request)
    query = (request.GET.get("q") or "").strip()
    exclude_slug = (request.GET.get("exclude") or "").strip()
    exclude = owner_albums.filter(slug=exclude_slug).first() if exclude_slug else None
    entries, total = albums_listing_page(listing, viewer, offset=offset, limit=limit, name_contains=query, exclude=exclude)
    rows = []
    for entry in entries:
        album_owner = _listed_album_owner(entry.album, listing, owner)
        rows.append(
            {
                "slug": entry.album.slug,
                "name": entry.album.name,
                "photo_count": entry.photo_count,
                "cover_url": entry.cover.thumb_url if entry.cover else "",
                "add_url": reverse(f"{_url_prefix(album_owner)}.add", args=[*_owner_url_args(album_owner), entry.album.slug]),
            }
        )
    next_offset = offset + len(entries)
    params: dict[str, str | int] = {"picker": 1}
    if query:
        params["q"] = query
    if exclude_slug:
        params["exclude"] = exclude_slug
    next_url = _query_url(_list_url(owner, include_children), **params, offset=next_offset) if entries and next_offset < total else ""
    return render(
        request,
        "dashboard/partials/albums/_album_picker_rows.html",
        {"picker_albums": rows, "query": query, "first_page": offset == 0, "next_url": next_url},
    )


def _attach_owner_action_urls(ctx: dict, owner: Pin | Wiki | Profile) -> None:
    """URLs the album UI needs for delete / send-to-wiki / share, when they exist.

    Three separate questions, and P61 is what came of answering them with one URL: a vault album got no
    bulk endpoint at all, so its Delete button rendered hidden forever alongside the two that genuinely
    do not apply.

    - Delete works for a pin and for the vault, at their own endpoints.
    - Send to wiki is pin-only: the endpoint derives the wiki from ``pin.location``, and a vault album
      has no location. The vault's own per-photo version asks the ...
    - Share opens the *pin* share dialog, so it is pin-only for the same reason. Single-photo share from
      the lightbox is how a vault photo gets shared, and is unaf...
    """
    if isinstance(owner, Pin):
        slug = _owner_slug(owner)
        ctx["gallery_bulk_url"] = reverse("pin.gallery.bulk", args=[slug])
        ctx["gallery_wiki_url"] = ctx["gallery_bulk_url"]
        ctx["pin_share_dialog_url"] = reverse("pin.share.dialog", args=[slug])
    elif isinstance(owner, Profile):
        ctx["gallery_bulk_url"] = reverse("vault.photos.bulk")
        ctx["gallery_wiki_url"] = ""
        ctx["pin_share_dialog_url"] = ""
    else:
        ctx["gallery_bulk_url"] = ""
        ctx["gallery_wiki_url"] = ""
        ctx["pin_share_dialog_url"] = ""
    ctx["label_image_url_template"] = reverse("label.image", args=["00000000-0000-0000-0000-000000000000"])


def _album_bulk_actions(*, inside_album: bool) -> list[dict]:
    """Buttons for the shared ``ul-bulk-bar`` on the Photos tab.

    The bar only shows a button when the client supplies a callback for its ``action`` key, so list and
    detail can share this list and hide move/remove on the album list by omitting those callbacks.
    """
    actions = [
        {"action": "add_to_album", "icon": "photo_library", "label": "Add to album"},
    ]
    if inside_album:
        actions.extend(
            [
                {"action": "set_cover", "icon": "wallpaper", "label": "Set as album cover"},
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
    GET /vault/photos/albums/

    ``?album=<slug>`` renders that album's own view instead of the list, which is what makes an opened
    album a real, shareable URL: the browser's Back button and a pasted link both land on the same
    place.
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> HttpResponse:
        """Render the Photos panel, or one album when ``?album=`` is given.

        An ``album`` slug that doesn't resolve falls back to the list rather than 404ing - a stale bookmark
        to a since-deleted album should still land somewhere useful.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            The rendered ``_albums_panel.html`` or ``_album_detail.html`` partial.
        """
        owner, qs = _resolve_album_owner(request, pin_slug, location_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        include_children = _include_children(request, owner)
        listing = _listing_owners(owner, include_children)

        if request.GET.get("picker"):
            return _album_picker_rows(request, owner, qs, listing, profile, include_children)

        if request.GET.get("albums"):
            return _album_grid_page(request, owner, listing, profile, include_children)

        if request.GET.get("external") or request.GET.get("external_section"):
            if not isinstance(owner, Pin):
                raise Http404
            return _external_response(request, owner, listing, profile, include_children)

        if request.GET.get("mine") and isinstance(owner, Pin):
            return _mine_page(request, listing, profile)

        if request.GET.get("loose"):
            offset, limit = _page_args(request)
            qs_images = loose_images_for(listing, profile)
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
            album = None
            album_pin_slug = request.GET.get("album_pin")
            if album_pin_slug and include_children and isinstance(owner, Pin):
                child = next((pin for pin in listing if isinstance(pin, Pin) and pin.slug == album_pin_slug), None)
                if child is not None:
                    album = Album.objects.for_pin(child).filter(slug=album_slug).first()
            if album is None:
                album = qs.filter(slug=album_slug).first()
            if album is not None:
                album_owner = album.parent_pin or album.parent_wiki or owner
                ctx = _album_detail_context(album_owner, album, profile)
                back = _safe_back_url(request.GET.get("back"))
                if back:
                    ctx["back_url"] = back
                    ctx["list_url"] = back
                return render(request, "dashboard/partials/albums/_album_detail.html", ctx)
        return _render_photos_panel(request, owner, profile)

    def post(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> HttpResponse:
        """Create a new album for this pin, wiki, or Vault.

        Args:
            request: HttpRequest, with ``name``/``description``/``kind`` fields.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            The re-rendered Photos panel, or a 400 for an invalid name/description.
        """
        owner, _qs = _resolve_album_owner(request, pin_slug, location_slug, vault=vault)
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
            sort=album_kind_spec(kind).default_sort,
            profile=profile,
            **owner_kwargs(owner),
        )
        return _render_photos_panel(request, owner, profile)


class AlbumDetailView(LoginRequiredMixin, View):
    """One album's own photo grid.

    GET /map/pin/<pin_slug>/albums/<album_slug>/
    GET /location/<location_slug>/wiki/albums/<album_slug>/
    GET /vault/photos/albums/<album_slug>/
    """

    def get(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> HttpResponse:
        """Render one album's contents.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to show.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            The rendered ``_album_detail.html`` partial.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        ctx = _album_detail_context(owner, album, profile)
        back = _safe_back_url(request.GET.get("back"))
        if back:
            ctx["back_url"] = back
            ctx["list_url"] = back
        from_pin = request.GET.get("from_pin")
        if from_pin and isinstance(owner, Pin):
            from urllib.parse import urlencode

            q = urlencode({"from_pin": from_pin, "children": "1"})
            ctx["delete_url"] = f"{ctx['delete_url']}?{q}"
        return render(request, "dashboard/partials/albums/_album_detail.html", ctx)


class AlbumEditView(LoginRequiredMixin, View):
    """Rename an album / change its blurb, kind, or cover photo.

    POST /map/pin/<pin_slug>/albums/<album_slug>/edit/
    POST /location/<location_slug>/wiki/albums/<album_slug>/edit/
    POST /vault/photos/albums/<album_slug>/edit/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> HttpResponse:
        """Apply an album edit.

        Only fields actually present in the POST (or JSON body) are touched, so a partial form can't blank
        out the rest.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to edit.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            The re-rendered Photos panel, JSON for a JSON request, or a 400.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        wants_json = "application/json" in (request.content_type or "")
        data = _parse_body(request) if wants_json else request.POST

        fields: list[str] = []
        if "name" in data:
            name = str(data.get("name") or "").strip()[:_MAX_ALBUM_NAME_LENGTH]
            if not name:
                return JsonResponse({"error": "Album name is required."}, status=400) if wants_json else HttpResponse("Album name is required.", status=400)
            album.name = name
            fields.append("name")
        if "description" in data:
            description = str(data.get("description") or "").strip()
            if (error := text_length_error(description, MAX_ALBUM_DESCRIPTION_LENGTH, "Description")) is not None:
                return JsonResponse({"error": error}, status=400) if wants_json else HttpResponse(error, status=400)
            album.description = description
            fields.append("description")
        if "kind" in data:
            kind = data.get("kind") or AlbumKind.PLAIN
            if kind in AlbumKind.values:
                album.kind = kind
                fields.append("kind")
        if "cover_image_id" in data:
            raw = data.get("cover_image_id")
            raw_s = "" if raw is None else str(raw)
            chosen = int(raw_s) if raw_s.isdigit() else None
            allowed = chosen is not None and visible_album_items(album, profile, owner).filter(image_id=chosen).exists()
            album.cover_image_id = chosen if allowed else None
            fields.append("cover_image")

        if fields:
            album.save(update_fields=[*fields, "updated"])
        if wants_json:
            return JsonResponse({"ok": True, "cover_image_id": album.cover_image_id})
        return _render_photos_panel(request, owner, profile)


class AlbumDeleteView(LoginRequiredMixin, View):
    """Delete an album. Its photos survive and fall back to the loose section.

    POST /map/pin/<pin_slug>/albums/<album_slug>/delete/
    POST /location/<location_slug>/wiki/albums/<album_slug>/delete/
    POST /vault/photos/albums/<album_slug>/delete/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> HttpResponse:
        """Delete the album.

        Args:
            request: HttpRequest.
            album_slug: Slug of the album to delete.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            The re-rendered Photos panel.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        album.delete()
        return _render_photos_panel(request, owner, profile)


class AlbumAddPhotosView(LoginRequiredMixin, View):
    """Add photos - already-local ones, or an external gallery item - to an album.

    POST /map/pin/<pin_slug>/albums/<album_slug>/add/
    POST /location/<location_slug>/wiki/albums/<album_slug>/add/
    POST /vault/photos/albums/<album_slug>/add/

    Body is JSON.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Add photos to the album.

        Args:
            request: HttpRequest with a JSON body.
            album_slug: Slug of the album to add to.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            JSON with how many photos were added, plus ``declined``/``error`` when an external item was
            skipped or failed to download.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        body = _parse_body(request)

        response: dict = {"added": 0}

        image_ids = _int_ids(body.get("image_ids"))
        if image_ids:
            # Re-scope through eligible_images_for so an id belonging to another owner entirely (a different
            # pin/wiki/profile, or one this viewer can't see) can't be filed into this album.
            images = list(eligible_images_for(owner, profile).filter(pk__in=image_ids))
            try:
                added = add_images_to_album(album, images, profile)
            except CapacityExceededError as exc:
                return JsonResponse({"error": exc.user_message}, status=409)
            response["added"] += added
            move_from = (body.get("move_from") or "").strip()
            source_album_id = None
            if move_from and move_from != album.slug:
                source = _qs.filter(slug=move_from).first()
                if source is not None:
                    response["removed"] = remove_images_from_album(source, image_ids)
                    source_album_id = source.pk
            if added:
                from urbanlens.dashboard.services.undo.mutations import stash_album_add

                stash_album_add(profile, album, [image.pk for image in images], source_album_id=source_album_id)

        media = body.get("media")
        if isinstance(media, dict) and media.get("url"):
            # The external Media-gallery search is place-scoped (it materializes against a Location) - a Vault
            # album has none, so this is refused outright rather than reaching _add_external's owner.location
            # access.
            if isinstance(owner, Profile):
                response["error"] = "Vault albums can only hold your own uploaded photos, not external media."
            else:
                result = self._add_external(owner, album, profile, media)
                response.update(result)

        return JsonResponse(response)

    def _add_external(self, owner: Pin | Wiki, album: Album, profile: Profile, media: dict) -> dict:
        """Vote an external gallery item relevant, then cache and file it.

        If the broker is unreachable the download falls back to running inline rather than silently never
        happening.

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

        queued = safely_enqueue_task(cache_media_item_into_album, album.pk, profile.pk, source, url, page_url=page_url, caption=caption, durable=False)
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
    POST /vault/photos/albums/<album_slug>/upload/

    Multipart, one ``image`` file per request - same contract as the pin and wiki galleries, whose
    response body the client reuses to render the new tile.
    The photo is created against the album's owner first, so it lands in the owner's gallery too; filing
    it in the album is the extra step.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Store the uploaded photo and file it in this album.

        Args:
            request: HttpRequest carrying the multipart ``image`` file.
            album_slug: Slug of the album to upload into.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            201 with the gallery JSON for the new photo, or the rejection's own status (400/409/413/415)
            with an ``error`` message.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            ensure_room(ALBUM_PHOTOS, album.pk)
        except CapacityExceededError as exc:
            return JsonResponse({"error": exc.user_message}, status=409)

        image, response = create_uploaded_photo(request, owner, profile, album=album)
        if image is not None:
            try:
                add_images_to_album(album, [image], profile)
            except CapacityExceededError as exc:
                # Filled by a concurrent add since the check above: the photo stays on its owner, unfiled.
                return JsonResponse({"error": exc.user_message}, status=409)
            from urbanlens.dashboard.services.undo.mutations import stash_album_add

            stash_album_add(profile, album, [image.pk])
            payload = json.loads(response.content)
            item = album.items.filter(image_id=image.pk).first()
            if item is not None:
                payload["item_id"] = item.pk
            payload["album_slug"] = album.slug
            return JsonResponse(payload, status=response.status_code)
        if response.status_code == 409:
            existing = existing_photo_for_upload(owner, profile, request.FILES.get("image"))
            if existing is not None:
                try:
                    add_images_to_album(album, [existing], profile)
                except CapacityExceededError as exc:
                    return JsonResponse({"error": exc.user_message}, status=409)
                from urbanlens.dashboard.services.undo.mutations import stash_album_add

                stash_album_add(profile, album, [existing.pk])
                payload = image_to_gallery_json(existing, request, profile)
                item = album.items.filter(image_id=existing.pk).first()
                if item is not None:
                    payload["item_id"] = item.pk
                payload["album_slug"] = album.slug
                return JsonResponse(payload)
        return response


class AlbumRemovePhotosView(LoginRequiredMixin, View):
    """Remove photos from an album without deleting the photos themselves.

    POST /map/pin/<pin_slug>/albums/<album_slug>/remove/
    POST /location/<location_slug>/wiki/albums/<album_slug>/remove/
    POST /vault/photos/albums/<album_slug>/remove/
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Remove photos from the album.

        Args:
            request: HttpRequest with a JSON ``image_ids`` list.
            album_slug: Slug of the album to remove from.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            JSON with how many membership rows were removed.
        """
        _owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_ids = _int_ids(_parse_body(request).get("image_ids"))
        removed = remove_images_from_album(album, image_ids)
        if removed:
            from urbanlens.dashboard.services.undo.mutations import stash_album_remove

            stash_album_remove(profile, album, image_ids)
        return JsonResponse({"removed": removed})


class AlbumItemsView(LoginRequiredMixin, View):
    """Paginated JSON of one album's photos, for the virtualized grid.

    GET /map/pin/<pin_slug>/albums/<album_slug>/items/
    GET /location/<location_slug>/wiki/albums/<album_slug>/items/
    GET /vault/photos/albums/<album_slug>/items/
    """

    def get(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Return one page of this album's viewer-visible photos.

        Args:
            request: HttpRequest with ``offset``/``limit`` query params.
            album_slug: Slug of the album to page.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
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


class AlbumEligibleImagesView(LoginRequiredMixin, View):
    """Paginated JSON of the photos this album could still take, for its picker.

    GET /map/pin/<pin_slug>/albums/<album_slug>/eligible/
    GET /location/<location_slug>/wiki/albums/<album_slug>/eligible/
    GET /vault/photos/albums/<album_slug>/eligible/

    Paginated rather than capped, so an older photo stays reachable.
    A cap would have been the smaller change and the wrong one: this picker's whole purpose can be "find
    the photo from last year", which is exactly what a newest-first slice removes.
    """

    def get(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Return one page of photos that may still be added to this album.

        Args:
            request: HttpRequest with ``offset``/``limit`` query params.
            album_slug: Slug of the album being added to.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        offset, limit = _page_args(request)
        already = list(album.items.values_list("image_id", flat=True))
        eligible = eligible_images_for(owner, profile).exclude(pk__in=already)
        total = eligible.count()
        page = list(eligible[offset : offset + limit].only("id", "uuid", "image", "thumbnail", "caption", "source_url"))
        return JsonResponse(
            {
                "items": [_photo_tile(image, request, profile) for image in page],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )


class AlbumReorderView(LoginRequiredMixin, View):
    """Persist a drag-and-drop reordering of an album's photos.

    POST /map/pin/<pin_slug>/albums/<album_slug>/reorder/
    POST /location/<location_slug>/wiki/albums/<album_slug>/reorder/
    POST /vault/photos/albums/<album_slug>/reorder/

    Body: ``{"items": [<AlbumItem id>, ...]}`` in the new display order.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None, vault: bool = False) -> JsonResponse:
        """Apply the new item order.

        Args:
            request: HttpRequest with a JSON ``items`` list of AlbumItem ids.
            album_slug: Slug of the album being reordered.
            pin_slug: Slug of the parent pin (personal route).
            location_slug: Slug of the parent location (community route).
            vault: True for a Vault (Profile-owned) album route.

        Returns:
            JSON with how many items were renumbered.
        """
        _owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug, vault=vault)
        submitted = _parse_body(request).get("items")
        # Counted before the ids are read: naming more items than an album may hold is not a reorder.
        ceiling = ALBUM_PHOTOS.ceiling()
        if isinstance(submitted, list) and len(submitted) > ceiling:
            return JsonResponse({"error": f"Reorder at most {ceiling} items at a time."}, status=400)
        item_ids = _int_ids(submitted)
        reordered = reorder_album_items(album, item_ids)
        return JsonResponse({"reordered": reordered})


class AlbumMoveView(LoginRequiredMixin, View):
    """Move a pin album onto another pin in the same parent/child tree.

    POST /map/pin/<pin_slug>/albums/<album_slug>/move/
    Body: ``{"pin_slug": "<target>"}``.
    """

    def post(self, request: HttpRequest, album_slug: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Re-parent the album and its photos onto the chosen pin.

        Args:
            request: HttpRequest with JSON ``pin_slug``.
            album_slug: Slug of the album to move.
            pin_slug: Slug of the album's current parent pin.
            location_slug: Unused; wiki albums cannot move.

        Returns:
            JSON with the (possibly new) album slug and target pin slug.
        """
        owner, _qs, album = _get_album(request, pin_slug, location_slug, album_slug)
        if not isinstance(owner, Pin):
            return JsonResponse({"error": "Community albums stay on their wiki."}, status=400)
        target_slug = str(_parse_body(request).get("pin_slug") or "").strip()
        target = Pin.objects.filter(slug=target_slug, profile_id=owner.profile_id).select_related("location").first()
        if target is None:
            return JsonResponse({"error": "That pin was not found."}, status=404)
        try:
            moved = move_album_to_pin(album, target)
        except ValueError as exc:
            logger.info("album %s move to pin %s rejected: %s", album.pk, target.pk, exc)
            return JsonResponse({"error": "That album couldn't be moved there."}, status=400)
        return JsonResponse({"ok": True, "slug": moved.slug, "pin_slug": target.slug})
