"""Georeferenced image overlays on a Pin's or Wiki's map.

A user drops a historical map image - a Sanborn fire-insurance sheet, a site
plan, an old survey - onto the live map and drags its four corners until it
lines up with the real streets. See
:class:`~urbanlens.dashboard.models.map_overlay.model.MapImageOverlay` for why
four free corners rather than a bounding box.

Three ways to supply the image, all landing on the same model:

* **Upload** - a file straight from the user's device.
* **Pick from this pin's/wiki's Media** - a gallery item (a Sanborn sheet from
  REData's Library of Congress imagery, a CRIS survey scan, ...). Gallery items
  are transient by design, so the picked one is materialized into a real
  ``Image`` first (``services.media.media_materialize``) - otherwise the
  overlay would break the moment the provider rotated its URL.
* **External URL** - referenced in place, for a user who would rather not
  store a copy. Validated against SSRF the same way every other user-supplied
  URL in this project is.

Same two-parents permission split as ``custom_layers``/``markup``: a
pin-scoped overlay is personal and editable only by its owner; a wiki-scoped
one is community data any signed-in user with wiki access may edit.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.map_overlay.model import CORNERS, MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.text_limits import column_max_length
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.models.map_overlay.queryset import MapImageOverlayQuerySet
    from urbanlens.dashboard.models.wiki.model import Wiki

#: Read from the column, not repeated: this truncates writes to
#: MapImageOverlay.name, so a widened column would otherwise keep clipping at
#: the old width with nothing to show why.
_MAX_NAME_LENGTH = column_max_length(MapImageOverlay, "name")
#: How many overlays one pin or wiki may hold. Each is a full-resolution image
#: composited on every map frame, so a page with dozens would be unusable long
#: before it hit any storage limit - this is a rendering budget, not a quota.
MAX_OVERLAYS_PER_MAP = 12

#: Stand-in uuid used to build a per-overlay URL *template* for the map JS,
#: which substitutes the real uuid client-side. Django's ``<uuid:...>``
#: converter will not reverse against a placeholder like "__uuid__", so a
#: real (and obviously synthetic) all-zero uuid stands in.
OVERLAY_UUID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def _resolve_owner(request: HttpRequest, pin_slug: str | None, location_slug: str | None) -> tuple[Pin | Wiki, MapImageOverlayQuerySet]:
    """Resolve the overlay owner (Pin or Wiki) from URL kwargs.

    Args:
        request: The current request, used for the ownership checks.
        pin_slug: Slug of the parent pin, on a personal-overlay route.
        location_slug: Slug of the parent location, on a community route.

    Returns:
        Tuple of (owner, overlay queryset already scoped to that owner).

    Raises:
        Http404: Neither slug was supplied, or the viewer may not touch it.
    """
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, MapImageOverlay.objects.for_pin(pin)
    if location_slug is None:
        raise Http404
    # Map annotation layers carry the same ruling as markup and detail pins:
    # hidden outright for a concealed viewer, whoever made them. The wiki page
    # already sends an empty list in its render context, but this endpoint is
    # fetched separately after load, so concealing only the context left the map
    # repopulating itself a moment later.
    from urbanlens.dashboard.services.wiki.concealment import concealment_active

    _location, wiki, profile = resolve_visible_wiki(request, location_slug)
    if concealment_active(wiki, profile):
        return wiki, MapImageOverlay.objects.none()
    return wiki, MapImageOverlay.objects.for_wiki(wiki)


def _owner_kwargs(owner: Pin | Wiki) -> dict:
    """The parent FK kwargs (``parent_pin``/``parent_wiki``) for ``owner``."""
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def _owner_location(owner: Pin | Wiki):
    """The ``Location`` behind either owner - both hold one."""
    return owner.location


def _parse_corners(raw: str | None) -> list[list[float]] | None:
    """Parse a posted ``corners`` JSON array into four ``[lat, lng]`` pairs.

    Args:
        raw: The raw request value, expected to be a JSON array of four
            two-element arrays.

    Returns:
        The parsed corners, or None when the value is absent or malformed -
        callers treat None as "leave the existing georeferencing alone" rather
        than writing a half-parsed position.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) != len(CORNERS):
        return None
    corners: list[list[float]] = []
    for entry in parsed:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return None
        try:
            latitude, longitude = float(entry[0]), float(entry[1])
        except (TypeError, ValueError):
            return None
        # Out-of-range coordinates would render as an invisible overlay
        # somewhere off the world rather than failing loudly, so they are
        # rejected here instead of being clamped into a position the user
        # never chose.
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            return None
        corners.append([latitude, longitude])
    return corners


def _clamped_opacity(raw: str | None, fallback: int) -> int:
    """Parse a posted opacity percent, falling back when absent/unparseable."""
    try:
        return max(0, min(100, int(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _image_from_request(request: HttpRequest, owner: Pin | Wiki, profile: Profile) -> tuple[object | None, str, str | None]:
    """Resolve the overlay's image source from an upload, a gallery pick, or a URL.

    Args:
        request: The current request.
        owner: The Pin or Wiki the overlay will belong to.
        profile: The acting profile (owns any uploaded/materialized Image).

    Returns:
        Tuple of (Image or None, external url or ``""``, error message or None).
    """
    from urbanlens.dashboard.services.media.previews import is_web_safe

    # An existing photo already on this pin/wiki, picked from the dialog's own
    # media grid - reused directly rather than re-downloaded/materialized like
    # a transient provider item below, since it is already a real, owned Image.
    image_id = (request.POST.get("image_id") or "").strip()
    if image_id:
        owner_filter = {"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}
        image = Image.objects.filter(pk=image_id, **owner_filter).first()
        if image is None:
            return None, "", "That photo could not be found."
        return image, "", None

    upload = request.FILES.get("image")
    if upload is not None:
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo

        # The canonical upload service, not a raw Image.objects.create: it
        # owns the quota check + per-profile lock (without which N concurrent
        # uploads all pass the check), checksum dedupe, file_size (without
        # which the sheet never counts against quota), and the async EXIF/
        # keyword ingestion every other upload gets.
        try:
            image = upload_photo(
                profile,
                upload,
                caption=(request.POST.get("name") or "").strip() or None,
                **({"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}),
            )
        except PhotoUploadError as exc:
            return None, "", exc.message
        return image, "", None

    # A Media-gallery pick. The gallery renders provider results live and
    # persists nothing per item, so the chosen one is downloaded into a real
    # Image here - referencing the provider URL directly would leave the
    # overlay broken as soon as that URL rotted.
    media_url = (request.POST.get("media_url") or "").strip()
    if media_url:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item

        try:
            image = materialize_media_item(
                location=_owner_location(owner),
                profile=profile,
                source=(request.POST.get("media_source") or "").strip(),
                url=media_url,
                page_url=(request.POST.get("media_page_url") or "").strip(),
                caption=(request.POST.get("name") or "").strip(),
                **({"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}),
            )
        except MaterializeError as exc:
            return None, "", str(exc)
        return image, "", None

    external_url = (request.POST.get("image_url") or "").strip()
    if external_url:
        from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url

        try:
            ensure_public_http_url(external_url, max_length=1000)
        except UnsafeUrlError as exc:
            return None, "", str(exc)
        if not is_web_safe(external_url):
            # An external URL is loaded by the browser directly (nothing
            # server-side ever fetches it), so a TIFF or PDF here would be a
            # silently blank overlay. Upload it instead and the normal media
            # pipeline can rasterize it.
            return None, "", "That link isn't an image a browser can display. Upload the file instead."
        return None, external_url, None

    return None, "", "Choose an image to overlay."


def overlay_payload(qs: MapImageOverlayQuerySet) -> list[dict]:
    """Serialize an owner's renderable overlays for the map.

    Shared with the pin-detail and wiki page controllers, which embed the
    same list on first render - so a page load and a later HTMX refresh
    hand the renderer identical shapes.

    Args:
        qs: An owner-scoped ``MapImageOverlay`` queryset.

    Returns:
        One ``to_json()`` dict per overlay that still has an image.
    """
    return [overlay.to_json() for overlay in qs.renderable().select_related("image", "layer").order_by("order", "id")]


def _render_overlay_list(request: HttpRequest, owner: Pin | Wiki, qs: MapImageOverlayQuerySet, error: str | None = None) -> HttpResponse:
    """Render the manage-overlays list, with an ``HX-Trigger`` for the map JS.

    Action URLs are built here by positional ``args`` (rather than reversed
    in-template) so the pin-vs-wiki URL-name difference stays invisible to the
    template - the same approach ``custom_layers._render_layer_list`` takes.

    Args:
        request: The current request.
        owner: The Pin or Wiki the overlays belong to.
        qs: That owner's overlay queryset.
        error: Message to surface as a toast, if the last action failed.

    Returns:
        The rendered partial, carrying ``ul:map-overlays-changed`` with the
        fresh overlay list so the map re-renders without a page reload.
    """
    is_pin = isinstance(owner, Pin)
    url_prefix = "pin.overlays" if is_pin else "location.wiki.overlays"
    owner_slug = owner.slug if is_pin else owner.location.slug

    overlays = list(qs.select_related("image", "layer").order_by("order", "id"))
    rows = [
        {
            "overlay": overlay,
            "edit_url": reverse(f"{url_prefix}.edit", args=[owner_slug, overlay.uuid]),
            "delete_url": reverse(f"{url_prefix}.delete", args=[owner_slug, overlay.uuid]),
        }
        for overlay in overlays
    ]
    response = render(
        request,
        "dashboard/partials/layout/_map_overlays_list.html",
        {
            "rows": rows,
            "create_url": reverse(url_prefix, args=[owner_slug]),
            # "This page's own media", for the picker - already-uploaded photos,
            # not the multi-provider gallery. The historical-maps section lives
            # outside this swapped fragment now (see _map_annotations_panels.html/
            # editor.html), so it isn't re-fetched from REData on every edit here.
            "gallery_json_url": reverse(f"{url_prefix}.media", args=[owner_slug]),
            "layers": CustomLayer.objects.filter(**_owner_kwargs(owner)).order_by("order", "id"),
            "at_limit": len(overlays) >= MAX_OVERLAYS_PER_MAP,
            "max_overlays": MAX_OVERLAYS_PER_MAP,
        },
    )
    response["HX-Trigger"] = json.dumps({"ul:map-overlays-changed": {"overlays": overlay_payload(qs)}, **({"ul:toast": {"message": error, "level": "error"}} if error else {})})
    return response


class OverlayMediaPickerView(LoginRequiredMixin, View):
    """This pin's/wiki's own already-uploaded photos, for the manage-overlays dialog's picker.

    Deliberately not ``pin.gallery.json``/``location.wiki.gallery.json``: those
    feed the pin's own photo *map layer*, so they filter to images that already
    have coordinates - which would hide every photo not yet geolocated,
    including one just uploaded here for use as an overlay.

    ``GET pin/<slug>/overlays/media/`` and the wiki counterpart.
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """List this owner's images, most recent first."""
        owner, _qs = _resolve_owner(request, pin_slug, location_slug)
        owner_filter = {"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}
        # visible_to for the same reason the wiki gallery uses it: contributing a
        # photo to a wiki does not withdraw what its uploader said about who may
        # see their photos, and this picker was the one wiki photo surface that
        # did not ask. On the pin branch it costs nothing - _resolve_owner has
        # already scoped that to the viewer's own pin, and visible_to always
        # includes the viewer's own images.
        # get_or_create, not request.user.profile: the reverse accessor raises
        # RelatedObjectDoesNotExist for a user whose profile row was never made,
        # and it is typed against an anonymous user this LoginRequired view can
        # never actually receive. Same resolution every other view uses.
        viewer, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(**owner_filter).exclude(image="").visible_to(viewer).order_by("-created")[:60]
        return JsonResponse({"images": [{"id": image.pk, "url": request.build_absolute_uri(image.image.url), "caption": image.caption or ""} for image in images]})


class MapOverlayListView(LoginRequiredMixin, View):
    """GET the manage-overlays list; POST to add one.

    ``GET/POST pin/<slug>/overlays/`` and the wiki counterpart.
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Render the manage-overlays dialog body."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug)
        return _render_overlay_list(request, owner, qs)

    def post(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Create one overlay from an upload, a Media-gallery pick, or a URL."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if qs.count() >= MAX_OVERLAYS_PER_MAP:
            return _render_overlay_list(request, owner, qs, error=f"A map can hold at most {MAX_OVERLAYS_PER_MAP} image overlays.")

        corners = _parse_corners(request.POST.get("corners"))
        if corners is None:
            return _render_overlay_list(request, owner, qs, error="Could not read where to place the overlay on the map.")

        image, image_url, error = _image_from_request(request, owner, profile)
        if error is not None:
            return _render_overlay_list(request, owner, qs, error=error)

        overlay = MapImageOverlay(
            name=(request.POST.get("name") or "").strip()[:_MAX_NAME_LENGTH],
            image=image,
            image_url=image_url,
            opacity=_clamped_opacity(request.POST.get("opacity"), 70),
            order=(qs.order_by("-order").values_list("order", flat=True).first() or 0) + 1,
            profile=profile,
            **_owner_kwargs(owner),
        )
        overlay.set_corners(corners)
        overlay.save()
        return _render_overlay_list(request, owner, qs)


class MapOverlayEditView(LoginRequiredMixin, View):
    """POST to update one overlay's presentation settings."""

    def post(self, request: HttpRequest, overlay_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Update name, opacity, lock state, visibility, order, and layer."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug)
        overlay = get_object_or_404(qs, uuid=overlay_uuid)

        overlay.name = (request.POST.get("name") or "").strip()[:_MAX_NAME_LENGTH]
        overlay.opacity = _clamped_opacity(request.POST.get("opacity"), overlay.opacity)
        overlay.locked = request.POST.get("locked") in ("1", "true", "on")
        overlay.default_visible = request.POST.get("default_visible") in ("1", "true", "on")
        with contextlib.suppress(TypeError, ValueError):
            overlay.order = int(request.POST.get("order", overlay.order))

        layer_uuid = (request.POST.get("layer") or "").strip()
        # Scoped to this owner's own layers, so a posted uuid can't attach the
        # overlay to some other pin's or wiki's layer.
        overlay.layer = CustomLayer.objects.filter(**_owner_kwargs(owner), uuid=layer_uuid).first() if layer_uuid else None

        overlay.save(update_fields=["name", "opacity", "locked", "default_visible", "order", "layer", "updated"])
        return _render_overlay_list(request, owner, qs)


class MapOverlayCornersView(LoginRequiredMixin, View):
    """POST new corner coordinates after the user drags a handle.

    Kept apart from :class:`MapOverlayEditView` because it fires on every
    drag-end while a user is aligning a sheet: it writes eight float columns
    and answers a small JSON body, rather than re-rendering the whole
    manage-overlays list on each nudge.
    """

    def post(self, request: HttpRequest, overlay_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Persist a dragged overlay's four corners."""
        _owner, qs = _resolve_owner(request, pin_slug, location_slug)
        overlay = get_object_or_404(qs, uuid=overlay_uuid)

        corners = _parse_corners(request.POST.get("corners"))
        if corners is None:
            return JsonResponse({"error": "Invalid corners."}, status=400)
        if overlay.locked:
            # The handles are hidden client-side when locked; refusing here too
            # means a stale page (locked in another tab) can't move it anyway.
            return JsonResponse({"error": "This overlay is locked."}, status=409)

        overlay.set_corners(corners)
        overlay.save(update_fields=[f"{name}_{axis}" for name in CORNERS for axis in ("latitude", "longitude")] + ["updated"])
        return JsonResponse({"corners": overlay.corners()})


class MapOverlayDeleteView(LoginRequiredMixin, View):
    """DELETE one overlay.

    The backing ``Image`` is deliberately left alone: an uploaded sheet also
    lives in the pin's or wiki's own gallery, and a materialized gallery pick
    may be shared with the wiki - deleting the overlay is about the map, not
    about discarding the photo.
    """

    def delete(self, request: HttpRequest, overlay_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Remove one overlay from its map."""
        owner, qs = _resolve_owner(request, pin_slug, location_slug)
        get_object_or_404(qs, uuid=overlay_uuid).delete()
        return _render_overlay_list(request, owner, qs)


#: Georeference transformations whose ``rmse_meters`` is not an accuracy figure.
#: A thin-plate spline interpolates its control points by construction, so its
#: residual is ~0 whatever the fit is actually like - reporting that as "±0 m"
#: would advertise a perfect placement for what may be the worst one in the
#: list. REData's own model docstring says so; this is the consumer honouring it.
_UNINFORMATIVE_RMSE_TRANSFORMS = frozenset({"thinPlateSpline"})

#: Above this, a georeference is placing the sheet by metres rather than
#: centimetres and the user should know before drawing a building on it. Below
#: it, the number is noise on a scanned historical map.
_NOTABLE_RMSE_METERS = 25.0


def georeference_accuracy(georeference: dict) -> str:
    """A short honest note about how well a sheet is placed, or ``""``.

    Args:
        georeference: REData's ``georeference`` block - ``transformation``,
            ``rmse_meters``, ``gcp_count``.

    Returns:
        Something like ``"±40 m (6 control points)"``, or ``""`` when the
        figure would be absent, meaningless, or too small to be worth the
        pixels.
    """
    if str(georeference.get("transformation") or "") in _UNINFORMATIVE_RMSE_TRANSFORMS:
        return ""
    rmse = georeference.get("rmse_meters")
    if not isinstance(rmse, (int, float)) or rmse < _NOTABLE_RMSE_METERS:
        return ""
    points = georeference.get("gcp_count")
    suffix = f" ({points} control points)" if isinstance(points, int) and points else ""
    return f"±{rmse:,.0f} m{suffix}"


def historical_map_row(match: dict) -> dict | None:
    """One picker row from a REData historical-map match, or None to skip it.

    Split out of the view because the POST path re-queries the same endpoint
    and has to agree with the list the user picked from.

    Reads three fields the picker previously cached and ignored. The thumbnail
    matters most: choosing between a dozen scanned sheets of one neighbourhood
    is a visual task, and a list of titles ("Sanborn Map of ...", eleven times)
    is not a way to do it. ``thumbnail_url`` and ``landing_page_url`` are the
    *institution's* own public URLs, not REData-authenticated ones, so they can
    be linked directly - unlike the tile template, which is proxied precisely
    because REData's key must not reach the browser.

    Args:
        match: One entry from ``RedataHistoricalMapsGateway.get_maps_covering``.

    Returns:
        A template-ready row, or None when the match cannot be drawn.
    """
    sheet = match.get("sheet") or {}
    georeference = match.get("georeference") or {}
    if not georeference.get("uuid") or not georeference.get("bounds"):
        return None
    return {
        "georeference_uuid": georeference["uuid"],
        "title": sheet.get("title") or "Untitled map",
        "date_text": sheet.get("date_text") or "",
        "kind": (sheet.get("kind") or "other").replace("_", " "),
        "attribution": sheet.get("attribution") or "",
        "contains_point": bool(match.get("contains_point")),
        "thumbnail_url": sheet.get("thumbnail_url") or "",
        "landing_page_url": sheet.get("landing_page_url") or "",
        "accuracy": georeference_accuracy(georeference),
    }


class HistoricalMapBrowseView(LoginRequiredMixin, View):
    """Browse REData's georeferenced historical maps covering this pin/wiki, and add one as an overlay.

    ``GET pin/<slug>/overlays/historical/`` (and the wiki counterpart) lists
    sheets whose georeferenced footprint covers or nears the location - fire
    insurance plans, cadastral atlases, panoramic views - already placed by
    real control points, so no corner-dragging is needed. ``POST`` with a
    ``georeference_uuid`` from that list creates a tile overlay for it.

    The overlay's ``tile_url_template`` points at UrbanLens's own tile proxy
    (``map.historical_tiles``) rather than REData's template - REData's API
    key must never reach the browser.
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Render the list of georeferenced sheets covering the owner's location, most detailed first."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway

        owner, _qs = _resolve_owner(request, pin_slug, location_slug)
        is_pin = isinstance(owner, Pin)
        url_prefix = "pin.overlays" if is_pin else "location.wiki.overlays"
        owner_slug = owner.slug if is_pin else owner.location.slug
        context: dict = {"post_url": reverse(f"{url_prefix}.historical", args=[owner_slug]), "maps": [], "error": None}

        if not redata_configured():
            context["error"] = "Historical map search isn't available on this install."
            return render(request, "dashboard/partials/layout/_historical_maps_list.html", context)

        location = _owner_location(owner)
        try:
            matches = RedataHistoricalMapsGateway().get_maps_covering(float(location.latitude), float(location.longitude), radius_meters=2000, limit=25)
        except LocationContextUnavailableError:
            context["error"] = "Historical map search is temporarily unavailable."
            return render(request, "dashboard/partials/layout/_historical_maps_list.html", context)

        context["maps"] = [row for row in (historical_map_row(match) for match in matches) if row is not None]
        return render(request, "dashboard/partials/layout/_historical_maps_list.html", context)

    def post(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Create a tile overlay for one georeferenced sheet from the GET list."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway

        owner, qs = _resolve_owner(request, pin_slug, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if qs.count() >= MAX_OVERLAYS_PER_MAP:
            return _render_overlay_list(request, owner, qs, error=f"A map can hold at most {MAX_OVERLAYS_PER_MAP} image overlays.")
        if not redata_configured():
            return _render_overlay_list(request, owner, qs, error="Historical map search isn't available on this install.")

        georeference_uuid = (request.POST.get("georeference_uuid") or "").strip()
        location = _owner_location(owner)
        # Re-query REData rather than trusting posted bounds/titles: the uuid
        # must actually be a sheet covering this location, and the canonical
        # metadata comes back with it.
        try:
            matches = RedataHistoricalMapsGateway().get_maps_covering(float(location.latitude), float(location.longitude), radius_meters=2000, limit=25)
        except LocationContextUnavailableError:
            return _render_overlay_list(request, owner, qs, error="Historical map search is temporarily unavailable.")
        match = next((m for m in matches if (m.get("georeference") or {}).get("uuid") == georeference_uuid), None)
        bounds = ((match or {}).get("georeference") or {}).get("bounds") or []
        if match is None or len(bounds) != 4:
            return _render_overlay_list(request, owner, qs, error="That historical map doesn't cover this location.")

        sheet = match.get("sheet") or {}
        min_lon, min_lat, max_lon, max_lat = bounds

        # reverse() can't emit literal {z}/{x}/{y}, so build with sentinels and
        # substitute - keeping the stored template tied to URL routing rather
        # than a hardcoded path prefix.
        tile_template = reverse("map.historical_tiles", args=[georeference_uuid, 0, 0, 0]).replace("/0/0/0.png", "/{z}/{x}/{y}.png")

        name_parts = [part for part in (sheet.get("title"), sheet.get("date_text")) if part]
        overlay = MapImageOverlay(
            name=" - ".join(name_parts)[:_MAX_NAME_LENGTH],
            tile_url_template=tile_template,
            opacity=_clamped_opacity(request.POST.get("opacity"), 70),
            order=(qs.order_by("-order").values_list("order", flat=True).first() or 0) + 1,
            # Pre-placed by its georeference: the corner handles don't apply,
            # so it is born locked; the corners record its bounds.
            locked=True,
            profile=profile,
            **_owner_kwargs(owner),
        )
        overlay.set_corners([[max_lat, min_lon], [max_lat, max_lon], [min_lat, max_lon], [min_lat, min_lon]])
        overlay.save()
        return _render_overlay_list(request, owner, qs)
