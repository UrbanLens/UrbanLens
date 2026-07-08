"""Image gallery controller - upload, list, reposition, and delete photos."""

from __future__ import annotations

from decimal import Decimal
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.images import compute_checksum, image_to_gallery_json
from urbanlens.dashboard.services.pagination import get_page

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 12


def _wiki_for_location(location: Location | None) -> Wiki | None:
    """Return the community Wiki for a Location, creating it lazily (or None)."""
    if location is None:
        return None
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return wiki


# -- Pin gallery --------------------------------------------------------------


class PinGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the pin detail page (loaded via HTMX)."""

    def _get_context(self, request: HttpRequest, pin_slug: str) -> dict:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(pin=pin).select_related("profile").visible_to(profile).order_by("-created")
        page_obj = get_page(request, images, _GALLERY_PAGE_SIZE)
        return {"pin": pin, "images": page_obj.object_list, "page_obj": page_obj, "profile": profile, "context_type": "pin"}

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        ctx = self._get_context(request, pin_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Upload an image to a pin. Rejects a file the uploader already has on this pin."""
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(pin=pin, profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo to this pin."}, status=409)

        img = Image.objects.create(
            image=image_file,
            pin=pin,
            wiki=_wiki_for_location(pin.location),
            location=pin.location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            checksum=checksum,
        )
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class PinGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the pin photo map layer."""

    def get(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(pin=pin).select_related("profile").visible_to(profile).with_coords()
        data = [image_to_gallery_json(img, request, profile) for img in images]
        return JsonResponse({"images": data})


class PinImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a pin."""

    def _get_image(self, image_id: int, pin_slug: str) -> Image:
        return get_object_or_404(Image, pk=image_id, pin__slug=pin_slug)

    def post(self, request: HttpRequest, pin_slug: str, image_id: int) -> JsonResponse:
        """Update lat/lng when the user drags the photo marker on the map."""
        img = self._get_image(image_id, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        try:
            data = json.loads(request.body)
            img.latitude = Decimal(str(data["latitude"]))
            img.longitude = Decimal(str(data["longitude"]))
            img.save(update_fields=["latitude", "longitude", "updated"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to update image %s on pin %s: %s", image_id, pin_slug, exc)
            return JsonResponse({"error": "Invalid request data."}, status=400)
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, pin_slug: str, image_id: int) -> HttpResponse:
        img = self._get_image(image_id, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)


# -- Location wiki gallery -----------------------------------------------------


class WikiGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the wiki page."""

    def _get_context(self, request: HttpRequest, location_slug: str) -> dict:
        location = get_object_or_404(Location, slug=location_slug)
        wiki = _wiki_for_location(location)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).order_by("-created")
        page_obj = get_page(request, images, _GALLERY_PAGE_SIZE)
        return {"location": location, "wiki": wiki, "images": page_obj.object_list, "page_obj": page_obj, "profile": profile, "context_type": "wiki"}

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        ctx = self._get_context(request, location_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        """Upload an image to a location wiki. Rejects a file the uploader already has on this wiki."""
        location = get_object_or_404(Location, slug=location_slug)
        wiki = _wiki_for_location(location)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(wiki=wiki, profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo to this wiki."}, status=409)

        img = Image.objects.create(
            image=image_file,
            wiki=wiki,
            location=location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            checksum=checksum,
        )
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class WikiGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the wiki photo map layer."""

    def get(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        location = get_object_or_404(Location, slug=location_slug)
        wiki = _wiki_for_location(location)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).with_coords()
        data = [image_to_gallery_json(img, request, profile) for img in images]
        return JsonResponse({"images": data})


class WikiImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a wiki."""

    def _get_image(self, image_id: int, location_slug: str) -> Image:
        return get_object_or_404(Image, pk=image_id, wiki__location__slug=location_slug)

    def post(self, request: HttpRequest, location_slug: str, image_id: int) -> JsonResponse:
        img = self._get_image(image_id, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        try:
            data = json.loads(request.body)
            img.latitude = Decimal(str(data["latitude"]))
            img.longitude = Decimal(str(data["longitude"]))
            img.save(update_fields=["latitude", "longitude", "updated"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to update image %s on location %s: %s", image_id, location_slug, exc)
            return JsonResponse({"error": "Invalid request data."}, status=400)
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, location_slug: str, image_id: int) -> HttpResponse:
        img = self._get_image(image_id, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)
