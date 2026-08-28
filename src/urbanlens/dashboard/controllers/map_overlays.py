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
import math
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.map_overlay.model import CORNERS, MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.text_limits import column_max_length
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.models.map_overlay.queryset import MapImageOverlayQuerySet

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
    # Filtered by who created it, not hidden outright - see the matching note
    # in controllers/custom_layers.py._resolve_layer_owner.
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    _location, wiki, profile = resolve_visible_wiki(request, location_slug)
    return wiki, visible_rows(MapImageOverlay.objects.for_wiki(wiki), wiki, profile)


def _owner_kwargs(owner: Pin | Wiki) -> dict:
    """The parent FK kwargs (``parent_pin``/``parent_wiki``) for ``owner``."""
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def _owner_location(owner: Pin | Wiki):
    """The ``Location`` behind either owner - both hold one."""
    return owner.location


def _wants_json(request: HttpRequest) -> bool:
    """True when the caller asked for JSON rather than the HTMX list partial.

    The manage-overlays dialog is HTMX (``HX-Request``) and always wants the
    swapped HTML. The pin-detail lightbox's "use as floorplan overlay" action
    fetches this same POST as JSON so it can redirect to the editor.

    Args:
        request: The current request.

    Returns:
        True when the response should be a JSON body instead of the list partial.
    """
    if request.headers.get("HX-Request"):
        return False
    return "application/json" in (request.headers.get("Accept") or "")


def _default_corners(owner: Pin | Wiki) -> list[list[float]] | None:
    """A small box around the pin/wiki, used when the client did not seed corners.

    The manage dialog normally fills the hidden ``corners`` field from the
    current map viewport just before submit. Keyboard-submit, a missed hook,
    or the lightbox (which has no live map) used to fail with nothing on the
    map - this lands the overlay on the property instead, where dragging the
    corners is a small adjustment.

    Args:
        owner: The Pin or Wiki the overlay will belong to.

    Returns:
        Four ``[lat, lng]`` pairs around the location, or None when the
        owner has no coordinates to seed from.
    """
    location = _owner_location(owner)
    if location is None or location.latitude is None or location.longitude is None:
        return None
    latitude = float(location.latitude)
    longitude = float(location.longitude)
    # ~60 m north/south. Longitude degrees shrink toward the poles, so the
    # east/west span is scaled to keep the box roughly square in metres.
    dlat = 0.00055
    dlng = dlat / max(0.2, math.cos(math.radians(latitude)))
    return [
        [latitude + dlat, longitude - dlng],
        [latitude + dlat, longitude + dlng],
        [latitude - dlat, longitude + dlng],
        [latitude - dlat, longitude - dlng],
    ]


def _overlay_picker_images(owner: Pin | Wiki, viewer: Profile):
    """Photos the manage-overlays picker (and ``image_id`` POST) may offer.

    Matches the pin/wiki gallery's notion of "this page's photos", including
    a pin's child-pin uploads and visit-attached photos, without the gallery's
    page size or the old 60-row cap that hid older uploads behind newer ones.

    Videos and documents are excluded: an overlay is drawn as an ``<img>``.

    Args:
        owner: The Pin or Wiki whose photos to list.
        viewer: The profile looking at the picker (visibility filtering).

    Returns:
        Photos newest first, including a pin's child-pin and visit-attached
        uploads.
    """
    if isinstance(owner, Pin):
        subtree = Pin.objects.filter(pk=owner.pk).with_descendants()
        images = Image.objects.filter(Q(pin__in=subtree) | Q(visit__pin__in=subtree))
    else:
        images = Image.objects.filter(wiki=owner)
    return images.filter(media_type=MediaKind.PHOTO).exclude(image="").visible_to(viewer).distinct().order_by("-created")


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
    # Scoped to the same queryset the picker lists, so a child-pin photo the
    # picker offered is actually usable, not a silent "could not be found".
    image_id = (request.POST.get("image_id") or "").strip()
    if image_id:
        image = _overlay_picker_images(owner, profile).filter(pk=image_id).first()
        if image is None:
            return None, "", "That photo could not be found."
        return image, "", None

    upload = request.FILES.get("image")
    if upload is not None:
        from urbanlens.dashboard.services.media.images import compute_checksum
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
            # Re-uploading a file already in the gallery used to fail with a
            # toast event nothing listened for, so the dialog just reset and
            # looked like a no-op. Reuse the existing row instead - the user
            # asked to overlay this image, not to store a second copy.
            if exc.status != 409:
                return None, "", exc.message
            existing = Image.objects.filter(profile=profile, checksum=compute_checksum(upload)).exclude(image="")
            image = _overlay_picker_images(owner, profile).filter(pk__in=existing).first() or existing.first()
            if image is None:
                return None, "", exc.message
            return image, "", None
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

    # A pasted external URL, materialized rather than referenced - the same
    # treatment a Media-gallery pick gets directly above, and for a stronger
    # reason than link rot. A stored foreign URL is handed to every viewer's
    # browser as an <img src>, so on a wiki - which anyone who can see the
    # place can add an overlay to, not just its author - it becomes a beacon:
    # the planter's server learns the IP, User-Agent and timing of everyone
    # who opens that specific page. Downloading it here means the column can
    # only ever hold a URL under our own MEDIA_URL.
    external_url = (request.POST.get("image_url") or "").strip()
    if external_url:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item
        from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url

        try:
            ensure_public_http_url(external_url, max_length=1000)
        except UnsafeUrlError as exc:
            return None, "", str(exc)
        if not is_web_safe(external_url):
            # A TIFF or PDF would be a silently blank overlay in the browser.
            # Upload it instead and the normal media pipeline can rasterize it.
            return None, "", "That link isn't an image a browser can display. Upload the file instead."
        try:
            image = materialize_media_item(
                location=_owner_location(owner),
                profile=profile,
                source="external_url",
                url=external_url,
                page_url="",
                caption=(request.POST.get("name") or "").strip(),
                **({"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}),
            )
        except MaterializeError as exc:
            return None, "", str(exc)
        return image, "", None

    return None, "", "Choose an image to overlay."


def overlay_payload(qs: MapImageOverlayQuerySet, visible_layer_ids: set[int] | None = None) -> list[dict]:
    """Serialize an owner's renderable overlays for the map.

    Shared with the pin-detail and wiki page controllers, which embed the
    same list on first render - so a page load and a later HTMX refresh
    hand the renderer identical shapes.

    Args:
        qs: An owner-scoped ``MapImageOverlay`` queryset, already narrowed to
            what this viewer may see (see ``_resolve_owner``).
        visible_layer_ids: The ``CustomLayer`` pks this viewer may list, when
            the owner is a wiki - wiki-scoped layer assignment isn't
            restricted to an overlay's own author, so an otherwise-visible
            overlay can still reference a layer this viewer cannot see. An
            overlay filed under one is reported as unlayered instead. None
            (the default) leaves every overlay's ``layer_uuid`` alone, which
            is correct for a pin-scoped owner - concealment never applies
            there, so every layer is always visible.

    Returns:
        One ``to_json()`` dict per overlay that still has an image.
    """
    entries = []
    for overlay in qs.renderable().select_related("image", "layer").order_by("order", "id"):
        entry = overlay.to_json()
        if visible_layer_ids is not None and overlay.layer_id is not None and overlay.layer_id not in visible_layer_ids:
            entry["layer_uuid"] = None
        entries.append(entry)
    return entries


def _visible_layer_ids(owner: Pin | Wiki, request: HttpRequest) -> set[int] | None:
    """The CustomLayer pks *request*'s viewer may see under *owner*, or None for a pin owner.

    Args:
        owner: The Pin or Wiki the overlays/layers belong to.
        request: The current request, for the viewer.

    Returns:
        None for a pin-scoped owner (concealment never applies there, so
        callers should leave layer references untouched); otherwise the set
        of layer pks this viewer may list - every layer's pk when
        concealment is off, since ``visible_rows`` is then a no-op.
    """
    if isinstance(owner, Pin):
        return None
    from urbanlens.dashboard.services.wiki.concealment import visible_rows

    profile, _ = Profile.objects.get_or_create(user=request.user)
    return set(visible_rows(CustomLayer.objects.filter(**_owner_kwargs(owner)), owner, profile).values_list("pk", flat=True))


def _render_overlay_list(
    request: HttpRequest,
    owner: Pin | Wiki,
    qs: MapImageOverlayQuerySet,
    error: str | None = None,
    toast: tuple[str, str] | None = None,
    align: str | None = None,
) -> HttpResponse:
    """Render the manage-overlays list, with an ``HX-Trigger`` for the map JS.

    Action URLs are built here by positional ``args`` (rather than reversed
    in-template) so the pin-vs-wiki URL-name difference stays invisible to the
    template - the same approach ``custom_layers._render_layer_list`` takes.

    Args:
        request: The current request.
        owner: The Pin or Wiki the overlays belong to.
        qs: That owner's overlay queryset.
        error: Message to surface as a toast and inline, if the last action failed.
        toast: Optional ``(message, level)`` for a non-error toast (e.g. a
            successful add). Ignored when ``error`` is set.
        align: Overlay uuid the map should immediately show corner handles for
            - a newly added sheet, so the user can warp it without hunting
            for the Align button.

    Returns:
        The rendered partial, carrying ``ul:map-overlays-changed`` with the
        fresh overlay list so the map re-renders without a page reload.
    """
    is_pin = isinstance(owner, Pin)
    url_prefix = "pin.overlays" if is_pin else "location.wiki.overlays"
    owner_slug = owner.slug if is_pin else owner.location.slug
    visible_layer_ids = _visible_layer_ids(owner, request)

    overlays = list(qs.select_related("image", "layer").order_by("order", "id"))
    rows = [
        {
            "overlay": overlay,
            "edit_url": reverse(f"{url_prefix}.edit", args=[owner_slug, overlay.uuid]),
            "delete_url": reverse(f"{url_prefix}.delete", args=[owner_slug, overlay.uuid]),
        }
        for overlay in overlays
    ]
    # The layer picker in this dialog offers the same set _resolve_layer_owner
    # would list for this viewer - a concealed viewer must not be offered a
    # stranger's layer name to file an overlay under, any more than
    # controllers.custom_layers would list it in the layers panel.
    layers_qs = CustomLayer.objects.filter(**_owner_kwargs(owner)).order_by("order", "id")
    if visible_layer_ids is not None:
        layers_qs = layers_qs.filter(pk__in=visible_layer_ids)
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
            "layers": layers_qs,
            "at_limit": len(overlays) >= MAX_OVERLAYS_PER_MAP,
            "max_overlays": MAX_OVERLAYS_PER_MAP,
            "error": error,
        },
    )
    changed: dict = {"overlays": overlay_payload(qs, visible_layer_ids)}
    if align:
        changed["align"] = align
    triggers: dict = {"ul:map-overlays-changed": changed}
    notice = (error, "error") if error else toast
    if notice:
        # showToast is the site-wide HX-Trigger the base template listens for.
        # ul:toast was a private name nobody handled, so add failures looked
        # like a silent no-op.
        triggers["showToast"] = {"message": notice[0], "level": notice[1]}
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _created_overlay_json(owner: Pin | Wiki, overlay: MapImageOverlay) -> JsonResponse:
    """JSON body for the lightbox's "use as floorplan overlay" fetch.

    Args:
        owner: The Pin or Wiki the overlay belongs to.
        overlay: The overlay that was created or already existed.

    Returns:
        ``ok``, the overlay uuid, and (for a pin) the floorplan editor URL
        with ``?align=`` so the editor opens already in warp mode.
    """
    payload: dict = {"ok": True, "uuid": str(overlay.uuid), "floorplan_url": ""}
    if isinstance(owner, Pin):
        payload["floorplan_url"] = f"{reverse('pin.floorplan', args=[owner.slug])}?align={overlay.uuid}"
    return JsonResponse(payload)


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
        images = _overlay_picker_images(owner, viewer)
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

        def fail(message: str) -> HttpResponse:
            if _wants_json(request):
                return JsonResponse({"error": message}, status=400)
            return _render_overlay_list(request, owner, qs, error=message)

        if qs.count() >= MAX_OVERLAYS_PER_MAP:
            return fail(f"A map can hold at most {MAX_OVERLAYS_PER_MAP} image overlays.")

        posted_corners = request.POST.get("corners")
        corners = _parse_corners(posted_corners)
        if corners is None:
            # Empty/missing is a client that skipped the viewport hook (Enter
            # in the name field, the lightbox). Garbage is not: placing a
            # half-parsed sheet somewhere the user didn't choose is worse than
            # refusing.
            if posted_corners:
                return fail("Could not read where to place the overlay on the map.")
            corners = _default_corners(owner)
            if corners is None:
                return fail("Could not read where to place the overlay on the map.")

        image, image_url, error = _image_from_request(request, owner, profile)
        if error is not None:
            return fail(error)

        # Re-adding a photo that is already an overlay is a place-this-sheet
        # request, not a duplicate row - send the user to warp the existing one.
        if image is not None:
            existing_overlay = qs.filter(image=image).first()
            if existing_overlay is not None:
                if _wants_json(request):
                    return _created_overlay_json(owner, existing_overlay)
                return _render_overlay_list(
                    request,
                    owner,
                    qs,
                    toast=("This photo is already an overlay. Drag its corners to line it up.", "info"),
                    align=str(existing_overlay.uuid),
                )

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
        if _wants_json(request):
            return _created_overlay_json(owner, overlay)
        return _render_overlay_list(
            request,
            owner,
            qs,
            toast=("Overlay added. Drag its four corners to line it up with the map.", "success"),
            align=str(overlay.uuid),
        )


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
        if layer_uuid:
            # Scoped to this owner's own layers (so a posted uuid can't
            # attach the overlay to some other pin's or wiki's layer) and,
            # on a wiki, to visible_rows - the dialog's own <select> only
            # ever offers visible options, so a posted uuid outside that set
            # can only be a stale or crafted request.
            layer_qs = CustomLayer.objects.filter(**_owner_kwargs(owner), uuid=layer_uuid)
            if isinstance(owner, Wiki):
                from urbanlens.dashboard.services.wiki.concealment import visible_rows

                profile, _ = Profile.objects.get_or_create(user=request.user)
                layer_qs = visible_rows(layer_qs, owner, profile)
            overlay.layer = layer_qs.first()
        elif overlay.layer_id is None or not isinstance(owner, Wiki):
            overlay.layer = None
        else:
            # The form always posts this field, and the <select> that fills
            # it only ever lists visible_layer_ids - so an empty value here
            # is indistinguishable from a concealed viewer's own read side
            # having nulled a real, invisible layer assignment for display
            # (see overlay_payload's visible_layer_ids parameter) and echoed
            # straight back by editing some other field. Only treat this as
            # a deliberate clear when the layer being cleared was one this
            # viewer could actually see and choose to remove.
            visible_ids = _visible_layer_ids(owner, request)
            if visible_ids is None or overlay.layer_id in visible_ids:
                overlay.layer = None

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
