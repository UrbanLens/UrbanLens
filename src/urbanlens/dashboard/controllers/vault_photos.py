"""Vault → Photos page: site-wide gallery, uploads, and organizing photos into visits."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any
import uuid as uuid_lib

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoMetadataConflict, PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.sort import GALLERY_SORT_SPECS, GallerySort, gallery_sort_spec
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.services.media.images import delete_stored_file, image_to_gallery_json
from urbanlens.dashboard.services.memories.photos import classify_photo, create_pin_and_log_visit, log_visit_on_pin
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins
from urbanlens.dashboard.services.visits.visits import accept_visit_suggestion, reject_visit_suggestion

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 24
_ATTENTION_LIMIT = 60


def _sorted_gallery(profile: Profile, request: HttpRequest):
    """The profile's uploaded-photo gallery, ordered by the requested ``sort`` param.

    Args:
        profile: Whose gallery to list.
        request: The current HttpRequest, read for ``sort``.

    Returns:
        The gallery queryset, ordered.
    """
    gallery = Image.objects.uploaded_by(profile).select_related("pin", "wiki")
    sort = gallery_sort_spec(request.GET.get("sort") or GallerySort.RECENT)
    return sort.apply(gallery)


def _parse_float(value: str | None) -> float | None:
    """Parse a POSTed coordinate string to float, or None if missing/malformed."""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attention_cards(profile: Profile) -> list[dict]:
    """Build the render context for each photo in the "needs attention" queue.

    Args:
        profile: The viewing profile whose unfiled photos to surface.

    Returns:
        A list of ``{"image", "state", "suggestion"}`` dicts, newest first,
        capped at ``_ATTENTION_LIMIT``. Only actionable states are included
        (``filed`` photos are dropped).
    """
    images = list(Image.objects.needs_attention(profile).select_related("location")[:_ATTENTION_LIMIT])
    pending = {
        s.origin_image_id: s
        for s in VisitSuggestion.objects.filter(
            origin_image__in=images,
            status=VisitSuggestionStatus.PENDING,
        ).select_related("location")
    }
    # These are all needs_attention photos (no visit, not dismissed), so a photo is
    # either awaiting a pending suggestion, geotagged-but-unpinned, or has no GPS -
    # derived here without a per-photo classify_photo() query.
    cards: list[dict] = []
    for image in images:
        suggestion = pending.get(image.pk)
        if suggestion is not None:
            state = "suggested"
        elif image.effective_latitude is not None and image.effective_longitude is not None:
            state = "needs_pin"
        else:
            state = "needs_location"
        cards.append({"image": image, "state": state, "suggestion": suggestion})
    return cards


def _photo_issues(profile: Profile) -> dict:
    """Pending upload failures and metadata conflicts for Vault → Photos."""
    failures = list(PhotoUploadFailure.objects.filter(profile=profile, status=PhotoIssueStatus.PENDING).select_related("pin", "album").order_by("-created")[:40])
    conflicts = list(PhotoMetadataConflict.objects.filter(profile=profile, status=PhotoIssueStatus.PENDING).select_related("existing_image", "new_image").order_by("-created")[:40])
    return {"upload_failures": failures, "metadata_conflicts": conflicts}


def _toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Uses the global ``showToast`` HX-Trigger handler wired up in ``themes/base.html``.

    Args:
        message: Text to display in the toast.
        level: toastr level (``success``/``info``/``warning``/``error``).
        status: HTTP status code.
        refresh_queue: When True, also fires ``refreshQueue`` so the whole
            organize queue re-fetches - used when an action may have changed
            *other* cards too (e.g. creating a pin can retroactively file or
            suggest other unfiled photos at the same place), not just the one
            being swapped out here.

    Returns:
        An empty-body response carrying an ``HX-Trigger`` header; swapping it with
        ``outerHTML`` removes the card from the queue while the toast fires.
    """
    triggers: dict[str, Any] = {"showToast": {"message": message, "level": level}}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _render_card(request: HttpRequest, image: Image, *, toast: str, level: str = "info") -> HttpResponse:
    """Re-render a photo card unchanged, with a toast - used when an action can't proceed.

    Args:
        request: The current request.
        image: The photo to re-render.
        toast: Toast message to fire.
        level: toastr level.

    Returns:
        The rendered card partial carrying a ``showToast`` HX-Trigger header.
    """
    suggestion = VisitSuggestion.objects.filter(origin_image=image, status=VisitSuggestionStatus.PENDING).select_related("location").first()
    state = "suggested" if suggestion else classify_photo(image)
    response = render(request, "dashboard/partials/vault/_photo_card.html", {"image": image, "state": state, "suggestion": suggestion})
    response["HX-Trigger"] = json.dumps({"showToast": {"message": toast, "level": level}})
    return response


class VaultPhotosView(LoginRequiredMixin, View):
    """The Vault Photos page - upload zone, organize queue, and full gallery.

    GET /vault/photos/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the Photos page.

        Args:
            request: The HTTP request.

        Returns:
            The rendered Photos page.
        """
        from urbanlens.dashboard.services.media.storage import get_quota_bytes, get_storage_totals, max_upload_file_size_bytes

        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = _sorted_gallery(profile, request)
        sort = request.GET.get("sort") or GallerySort.RECENT
        images = list(gallery[:_GALLERY_PAGE_SIZE])
        used_bytes, exempt_bytes = get_storage_totals(profile)
        return render(
            request,
            "dashboard/pages/vault/photos.html",
            {
                "page_name": "vault",
                "attention_cards": _attention_cards(profile),
                **_photo_issues(profile),
                "images": images,
                "profile": profile,
                "photo_count": gallery.count(),
                "unlogged_visits_count": len(unlogged_visited_pins(profile)),
                "storage_used_bytes": used_bytes,
                "storage_quota_bytes": get_quota_bytes(profile),
                "storage_exempt_bytes": exempt_bytes,
                "max_upload_file_size_bytes": max_upload_file_size_bytes(),
                "grid_page_size": _GALLERY_PAGE_SIZE,
                "sort": sort,
                "gallery_sort_specs": list(GALLERY_SORT_SPECS.values()),
            },
        )


class PhotoQueueView(LoginRequiredMixin, View):
    """The "needs attention" queue, re-fetched after uploads as ingestion lands.

    GET /vault/photos/queue/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render just the organize-queue partial.

        Args:
            request: The HTTP request.

        Returns:
            The rendered attention-queue partial.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/partials/vault/_photo_attention.html",
            {"attention_cards": _attention_cards(profile), "profile": profile, **_photo_issues(profile)},
        )


class PhotoItemsView(LoginRequiredMixin, View):
    """One page of the full gallery grid as JSON, for the windowed grid.

    GET /vault/photos/items/?offset=&limit=&sort=

    Same ``{items, total, offset, limit}`` shape as the album grid's
    ``AlbumItemsView`` (see controllers.albums), so both grids share one
    fetch/scroll/prune engine on the client - see
    frontend/ts/shared/photo-virtual-grid.ts.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return one page of the profile's gallery, in the requested sort.

        Args:
            request: The HTTP request, with ``offset``/``limit``/``sort`` query params.

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            offset = max(0, int(request.GET.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(request.GET.get("limit") or _GALLERY_PAGE_SIZE)
        except (TypeError, ValueError):
            limit = _GALLERY_PAGE_SIZE
        limit = min(max(1, limit), 100)

        gallery = _sorted_gallery(profile, request)
        total = gallery.count()
        images = list(gallery[offset : offset + limit])
        return JsonResponse(
            {
                "items": [image_to_gallery_json(image, request, profile) for image in images],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )


class PhotoUploadView(LoginRequiredMixin, View):
    """Upload one photo to the Vault gallery (called once per file by the page JS).

    POST /vault/photos/upload/
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Create an unfiled Image and kick off background metadata ingestion.

        Args:
            request: The HTTP request carrying an ``image`` file.

        Returns:
            The new image serialized for the gallery grid, or a 400 error.
        """
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo

        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        try:
            img = upload_photo(profile, image_file)
        except PhotoUploadError as exc:
            return JsonResponse({"error": exc.message}, status=exc.status)

        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class PhotoActionView(LoginRequiredMixin, View):
    """Organize actions on a single photo, each returning an HTMX card-removing response.

    POST /vault/photos/<image_id>/<action>/
    where action is one of accept, reject, create-pin, log-visit, dismiss, delete.
    """

    def _get_image(self, request: HttpRequest, image_id: int) -> tuple[Image, Profile]:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image = get_object_or_404(Image, pk=image_id)
        if image.profile_id != profile.pk:
            raise Http404
        return image, profile

    def _pending_suggestion(self, image: Image) -> VisitSuggestion | None:
        return VisitSuggestion.objects.filter(origin_image=image, status=VisitSuggestionStatus.PENDING).first()

    def accept(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Confirm a photo-origin suggestion, logging the visit and attaching the photo."""
        suggestion = self._pending_suggestion(image)
        if suggestion is None:
            return _render_card(request, image, toast="That suggestion is no longer available.")
        if accept_visit_suggestion(suggestion, profile) is None:
            return _render_card(request, image, toast="Visit logging is turned off - enable it in Settings to add this to your visit history.")
        return _toast("Added to your visit history.", refresh_queue=True)

    def reject(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Reject a photo-origin suggestion."""
        suggestion = self._pending_suggestion(image)
        if suggestion is not None:
            reject_visit_suggestion(suggestion)
        return _toast("Suggestion dismissed.", "info", refresh_queue=True)

    def create_pin(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Create a pin and log a visit, honouring the confirmation dialog's placement.

        The confirmation dialog posts the (possibly dragged) ``latitude``/``longitude``
        and an optional ``name``. When those are absent - e.g. a legacy one-click
        request - the photo's own coordinates are used.
        """
        if image.pin_id:
            # Another card's create-pin/log-visit call can retroactively file this
            # photo out from under a queue the client hasn't refreshed yet (see
            # create_pin_and_log_visit's resuggestion pass) - avoid logging a
            # redundant second visit for a stale click.
            return _toast("This photo has already been filed.", "info", refresh_queue=True)
        lat = _parse_float(request.POST.get("latitude"))
        lng = _parse_float(request.POST.get("longitude"))
        if lat is None or lng is None:
            lat = float(image.effective_latitude) if image.effective_latitude is not None else None
            lng = float(image.effective_longitude) if image.effective_longitude is not None else None
        if lat is None or lng is None:
            return _render_card(request, image, toast="This photo has no location.", level="error")
        # name is sanitized in Pin.save() (see naming.sanitize_name), not here.
        _, visit = create_pin_and_log_visit(profile, image, latitude=lat, longitude=lng, name=request.POST.get("name"))
        if visit is None:
            return _toast("Pin created. Visit logging is turned off, so no visit was recorded.", "info", refresh_queue=True)
        return _toast("Pin created and visit logged.", refresh_queue=True)

    def log_visit(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Log a visit on the pin the user chose in the manual search."""
        pin_slug = request.POST.get("pin_slug") or ""
        # The shared location-search engine identifies pins by slug, falling back to
        # the uuid when a pin has no slug (see AutocompleteResult.pin_slug) - accept
        # either form here rather than only the slug.
        pin_filter = Q(slug=pin_slug)
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            pin_filter |= Q(uuid=uuid_lib.UUID(pin_slug))
        pin = Pin.objects.filter(pin_filter, profile=profile).first()
        if pin is None:
            return _render_card(request, image, toast="That pin could not be found.", level="error")
        visit = log_visit_on_pin(profile, image, pin)
        if visit is None:
            return _toast("Photo filed. Visit logging is turned off, so no visit was recorded.", "info", refresh_queue=True)
        return _toast("Visit logged.", refresh_queue=True)

    def dismiss(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Clear a photo out of the organize queue without deleting it."""
        Image.objects.filter(pk=image.pk).update(organize_dismissed=True)
        return _toast("Photo dismissed.", "info", refresh_queue=True)

    def delete_photo(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Delete the photo entirely."""
        delete_stored_file(image)
        image.delete()
        return _toast("Photo deleted.", "info", refresh_queue=True)

    _ACTIONS = {
        "accept": accept,
        "reject": reject,
        "create-pin": create_pin,
        "log-visit": log_visit,
        "dismiss": dismiss,
        "delete": delete_photo,
    }

    def post(self, request: HttpRequest, image_id: int, action: str) -> HttpResponse:
        """Dispatch to the handler named by ``action``.

        Args:
            request: The HTTP request.
            image_id: PK of the photo being acted on.
            action: The organize action to perform.

        Returns:
            An HTMX card-removing response, or 404 for an unknown action.
        """
        handler = self._ACTIONS.get(action)
        if handler is None:
            raise Http404
        image, profile = self._get_image(request, image_id)
        try:
            return handler(self, request, image, profile)
        except Exception:
            # Any of these handlers can hit an unexpected DB/API failure - always
            # surface it as a toast (per project UI standards) instead of letting
            # it fall through to a bare 500 the client may not report cleanly.
            logger.exception("Photo action '%s' failed for image %s", action, image_id)
            return _render_card(request, image, toast="Something went wrong. Please try again.", level="error")


class PhotoPinSearchView(LoginRequiredMixin, View):
    """Autocomplete over the user's own pins, for manually filing a photo.

    GET /vault/photos/pin-search/?q=&image_id=
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render pin suggestions as file-to-this-pin buttons.

        Args:
            request: The HTTP request carrying ``q`` and ``image_id``.

        Returns:
            The rendered pin-search results partial.
        """
        from urbanlens.dashboard.services.map_pins.autocomplete import search_local

        profile, _ = Profile.objects.get_or_create(user=request.user)
        query = (request.GET.get("q") or "").strip()
        image_id = request.GET.get("image_id")
        results = [r for r in search_local(query, profile) if r.type == "pin" and r.pin_slug] if len(query) >= 2 else []
        return render(
            request,
            "dashboard/partials/vault/_pin_search_results.html",
            {"results": results, "image_id": image_id, "query": query},
        )


class PhotoPinConfirmView(LoginRequiredMixin, View):
    """Render the "confirm where this pin goes" dialog body for a geotagged photo.

    GET /vault/photos/<image_id>/confirm-pin/

    Shown before creating a pin from a photo that matches none of the user's
    existing pins, so they can see the location, drag the marker, name it, or
    change their mind and file the photo onto a different pin/place instead.
    """

    def get(self, request: HttpRequest, image_id: int) -> HttpResponse:
        """Render the placement/naming form for the photo's pin.

        Args:
            request: The HTTP request.
            image_id: PK of the geotagged photo a pin is being created for.

        Returns:
            The rendered confirmation partial, or 404 if the photo isn't the
            viewer's or has no coordinates to place a marker at.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image = get_object_or_404(Image.objects.select_related("location"), pk=image_id)
        if image.profile_id != profile.pk or image.effective_latitude is None or image.effective_longitude is None:
            raise Http404
        return render(request, "dashboard/partials/vault/_photo_pin_confirm.html", {"image": image})


class PhotoUploadFailureCreateView(LoginRequiredMixin, View):
    """Record a client-side load/processing failure so it can be retried later.

    POST /vault/photos/failures/
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Store filename + error for the current profile."""
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            body = json.loads(request.body or b"{}")
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid request data."}, status=400)
        filename = str(body.get("filename") or "photo")[:255]
        error = str(body.get("error") or "This photo couldn't be shown.")
        from urbanlens.dashboard.services.photos.uploads import record_photo_upload_failure

        pin = None
        pin_slug = str(body.get("pin_slug") or "").strip()
        if pin_slug:
            pin = Pin.objects.filter(slug=pin_slug, profile=profile).first()
        record_photo_upload_failure(profile, filename, error, pin=pin)
        return JsonResponse({"ok": True})


class PhotoUploadFailureDismissView(LoginRequiredMixin, View):
    """Dismiss a recorded upload failure from the Vault."""

    def post(self, request: HttpRequest, failure_id: int) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        failure = get_object_or_404(PhotoUploadFailure, pk=failure_id, profile=profile)
        failure.status = PhotoIssueStatus.DISMISSED
        failure.save(update_fields=["status", "updated"])
        return _toast("Dismissed.", refresh_queue=True)


class PhotoMetadataConflictResolveView(LoginRequiredMixin, View):
    """Apply the owner's metadata picks to every copy of that photo."""

    def post(self, request: HttpRequest, conflict_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.photos.uploads import resolve_photo_metadata_conflict

        profile, _ = Profile.objects.get_or_create(user=request.user)
        conflict = get_object_or_404(
            PhotoMetadataConflict.objects.select_related("existing_image"),
            pk=conflict_id,
            profile=profile,
            status=PhotoIssueStatus.PENDING,
        )
        try:
            body = json.loads(request.body or b"{}")
        except (TypeError, ValueError):
            body = request.POST
        choices: dict[str, int] = {}
        raw_choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(raw_choices, dict):
            for key, value in raw_choices.items():
                try:
                    choices[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        else:
            for key, value in request.POST.items():
                if key.startswith("field_"):
                    try:
                        choices[key.removeprefix("field_")] = int(value)
                    except (TypeError, ValueError):
                        continue
        resolve_photo_metadata_conflict(conflict, choices)
        return _toast("Photo details updated.", refresh_queue=True)


class PhotoMetadataConflictDismissView(LoginRequiredMixin, View):
    """Dismiss a metadata conflict without changing any photo."""

    def post(self, request: HttpRequest, conflict_id: int) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        conflict = get_object_or_404(PhotoMetadataConflict, pk=conflict_id, profile=profile)
        conflict.status = PhotoIssueStatus.DISMISSED
        conflict.save(update_fields=["status", "updated"])
        return _toast("Dismissed.", refresh_queue=True)
