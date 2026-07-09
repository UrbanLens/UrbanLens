"""Memories → Photos page: site-wide gallery, uploads, and organizing photos into visits."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.services.images import compute_checksum, image_to_gallery_json
from urbanlens.dashboard.services.memories.photos import classify_photo, create_pin_and_log_visit, log_visit_on_pin
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.visits import accept_visit_suggestion, reject_visit_suggestion

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 24
_ATTENTION_LIMIT = 60


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


def _toast(message: str, level: str = "success", *, status: int = 200) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Uses the global ``showToast`` HX-Trigger handler wired up in ``themes/base.html``.

    Args:
        message: Text to display in the toast.
        level: toastr level (``success``/``info``/``warning``/``error``).
        status: HTTP status code.

    Returns:
        An empty-body response carrying an ``HX-Trigger`` header; swapping it with
        ``outerHTML`` removes the card from the queue while the toast fires.
    """
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps({"showToast": {"message": message, "level": level}})
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
    response = render(request, "dashboard/partials/memories/_photo_card.html", {"image": image, "state": state, "suggestion": suggestion})
    response["HX-Trigger"] = json.dumps({"showToast": {"message": toast, "level": level}})
    return response


class MemoriesPhotosView(LoginRequiredMixin, View):
    """The Photos subpage of Memories - upload zone, organize queue, and full gallery.

    GET /memories/photos/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the Photos page.

        Args:
            request: The HTTP request.

        Returns:
            The rendered Photos page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = Image.objects.uploaded_by(profile).select_related("pin", "wiki")
        page_obj = get_page(request, gallery, _GALLERY_PAGE_SIZE)
        return render(
            request,
            "dashboard/pages/memories/photos.html",
            {
                "page_name": "memories",
                "attention_cards": _attention_cards(profile),
                "images": page_obj.object_list,
                "page_obj": page_obj,
                "profile": profile,
                "photo_count": gallery.count(),
                "unlogged_visits_count": len(unlogged_visited_pins(profile)),
            },
        )


class PhotoQueueView(LoginRequiredMixin, View):
    """The "needs attention" queue, re-fetched after uploads as ingestion lands.

    GET /memories/photos/queue/
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
            "dashboard/partials/memories/_photo_attention.html",
            {"attention_cards": _attention_cards(profile), "profile": profile},
        )


class PhotoGridPageView(LoginRequiredMixin, View):
    """One page of the full gallery grid, for infinite scroll.

    GET /memories/photos/page/?page=N
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the next gallery grid slice.

        Args:
            request: The HTTP request.

        Returns:
            The rendered grid-slice partial.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = Image.objects.uploaded_by(profile).select_related("pin", "wiki")
        page_obj = get_page(request, gallery, _GALLERY_PAGE_SIZE)
        return render(
            request,
            "dashboard/partials/memories/_photo_grid.html",
            {"images": page_obj.object_list, "page_obj": page_obj, "profile": profile},
        )


class PhotoUploadView(LoginRequiredMixin, View):
    """Upload one photo to the Memories gallery (called once per file by the page JS).

    POST /memories/photos/upload/
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Create an unfiled Image and kick off background metadata ingestion.

        Args:
            request: The HTTP request carrying an ``image`` file.

        Returns:
            The new image serialized for the gallery grid, or a 400 error.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        if not (image_file.content_type or "").startswith("image/"):
            return JsonResponse({"error": "That file is not an image."}, status=400)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo."}, status=409)

        img = Image.objects.create(image=image_file, profile=profile, checksum=checksum)

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class PhotoActionView(LoginRequiredMixin, View):
    """Organize actions on a single photo, each returning an HTMX card-removing response.

    POST /memories/photos/<image_id>/<action>/
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
        accept_visit_suggestion(suggestion, profile)
        return _toast("Added to your visit history.")

    def reject(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Reject a photo-origin suggestion."""
        suggestion = self._pending_suggestion(image)
        if suggestion is not None:
            reject_visit_suggestion(suggestion)
        return _toast("Suggestion dismissed.", "info")

    def create_pin(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Create a pin and log a visit, honouring the confirmation dialog's placement.

        The confirmation dialog posts the (possibly dragged) ``latitude``/``longitude``
        and an optional ``name``. When those are absent - e.g. a legacy one-click
        request - the photo's own coordinates are used.
        """
        lat = _parse_float(request.POST.get("latitude"))
        lng = _parse_float(request.POST.get("longitude"))
        if lat is None or lng is None:
            lat = float(image.effective_latitude) if image.effective_latitude is not None else None
            lng = float(image.effective_longitude) if image.effective_longitude is not None else None
        if lat is None or lng is None:
            return _render_card(request, image, toast="This photo has no location.", level="error")
        # TODO: We must sanitize the name to prevent XSS attacks.
        create_pin_and_log_visit(profile, image, latitude=lat, longitude=lng, name=request.POST.get("name"))
        return _toast("Pin created and visit logged.")

    def log_visit(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Log a visit on the pin the user chose in the manual search."""
        pin_slug = request.POST.get("pin_slug")
        pin = Pin.objects.filter(slug=pin_slug, profile=profile).first()
        if pin is None:
            return _render_card(request, image, toast="That pin could not be found.", level="error")
        log_visit_on_pin(profile, image, pin)
        return _toast("Visit logged.")

    def dismiss(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Clear a photo out of the organize queue without deleting it."""
        Image.objects.filter(pk=image.pk).update(organize_dismissed=True)
        return _toast("Photo cleared from your to-do list.", "info")

    def delete_photo(self, request: HttpRequest, image: Image, profile: Profile) -> HttpResponse:
        """Delete the photo entirely."""
        image.image.delete(save=False)
        image.delete()
        return _toast("Photo deleted.", "info")

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
        return handler(self, request, image, profile)


class PhotoPinSearchView(LoginRequiredMixin, View):
    """Autocomplete over the user's own pins, for manually filing a photo.

    GET /memories/photos/pin-search/?q=&image_id=
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
            "dashboard/partials/memories/_pin_search_results.html",
            {"results": results, "image_id": image_id, "query": query},
        )


class PhotoPinConfirmView(LoginRequiredMixin, View):
    """Render the "confirm where this pin goes" dialog body for a geotagged photo.

    GET /memories/photos/<image_id>/confirm-pin/

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
        return render(request, "dashboard/partials/memories/_photo_pin_confirm.html", {"image": image})
