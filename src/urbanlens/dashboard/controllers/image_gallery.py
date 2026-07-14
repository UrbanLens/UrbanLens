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
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.images import compute_checksum, image_to_gallery_json
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.storage import quota_error_for_upload
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 12


def _wiki_for_location(location: Location | None) -> Wiki | None:
    """Return the community Wiki for a Location, or None when it has no wiki yet."""
    return Wiki.objects.get_for_location(location)


# -- Pin gallery --------------------------------------------------------------


def _pin_gallery_images(request: HttpRequest, pin: Pin, profile: Profile):
    """Images for a pin's gallery, optionally including child-pin photos.

    With ``?children=1`` (the pin page's "show sub pin details" toggle) photos
    uploaded to any descendant child pin are included too, so the parent's
    gallery shows the whole place.

    Args:
        request: Current request (read for the ``children`` flag).
        pin: The pin whose gallery is being rendered.
        profile: The requesting profile (visibility filtering).

    Returns:
        Tuple of (queryset, include_children flag).
    """
    # Child expansion is owner-only: the "show sub pin details" toggle exists
    # on the owner's own pin page, and another user's child pins are theirs.
    include_children = request.GET.get("children") == "1" and pin.profile_id == profile.pk
    if include_children:
        subtree = Pin.objects.filter(pk=pin.pk).with_descendants()
        images = Image.objects.filter(pin__in=subtree).select_related("profile", "pin", "pin__location", "pin__location__wiki")
    else:
        images = Image.objects.filter(pin=pin).select_related("profile")
    return images.visible_to(profile), include_children


class PinGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the pin detail page (loaded via HTMX)."""

    def _get_context(self, request: HttpRequest, pin_slug: str) -> dict:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images, include_children = _pin_gallery_images(request, pin, profile)
        page_obj = get_page(request, images.order_by("-created"), _GALLERY_PAGE_SIZE)
        return {
            "pin": pin,
            "images": page_obj.object_list,
            "page_obj": page_obj,
            "profile": profile,
            "context_type": "pin",
            "include_children": include_children,
            "extra_query": "children=1" if include_children else "",
            "photo_bulk_actions": [
                {"action": "delete", "icon": "delete", "label": "Delete"},
                {"action": "wiki", "icon": "public", "label": "Send to wiki"},
            ],
        }

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        ctx = self._get_context(request, pin_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Upload an image to a pin. Rejects a file the uploader already has on this pin."""
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(pin=pin, profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo to this pin."}, status=409)
        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        img = Image.objects.create(
            image=image_file,
            pin=pin,
            wiki=_wiki_for_location(pin.location),
            location=pin.location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            checksum=checksum,
            file_size=image_file.size,
        )
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class PinGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the pin photo map layer."""

    def get(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images, include_children = _pin_gallery_images(request, pin, profile)
        data = []
        for img in images.with_coords():
            entry = image_to_gallery_json(img, request, profile)
            if include_children and img.pin_id is not None and img.pin_id != pin.pk and img.pin is not None:
                # Child-pin photos render read-only on the parent's map layer;
                # they are repositioned from their own pin's page.
                entry["child_pin_name"] = img.pin.effective_name
            data.append(entry)
        return JsonResponse({"images": data})


class PinGalleryBulkView(LoginRequiredMixin, View):
    """Bulk actions over a pin's own gallery photos: delete, or send to wiki.

    Backs the Photo gallery's multi-select floating toolbar. Only the
    profile's own uploads on this pin are eligible - selecting someone
    else's (child-pin, in the ``?children=1`` view) photo id is silently
    ignored rather than erroring, since the toolbar only ever offers these
    actions on the viewer's own tiles.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            data = json.loads(request.body)
            action = data["action"]
            image_ids = [int(i) for i in data.get("image_ids", [])]
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        images = Image.objects.filter(pk__in=image_ids, pin=pin, profile=profile)

        if action == "delete":
            count = 0
            for img in images:
                img.image.delete(save=False)
                img.delete()
                count += 1
            return JsonResponse({"deleted": count})

        if action == "send_to_wiki":
            wiki = _wiki_for_location(pin.location)
            if wiki is None:
                return JsonResponse({"error": "Create a community wiki for this location first."}, status=400)
            count = images.exclude(wiki=wiki).update(wiki=wiki)
            return JsonResponse({"updated": count})

        return JsonResponse({"error": "Unknown action."}, status=400)


class PinCoverPhotoView(LoginRequiredMixin, View):
    """Set or clear a pin's hero-banner cover photo."""

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Set (or, given a null ``image_id``, clear) the pin's cover photo.

        Args:
            request: Incoming HTTP request. Reads JSON body ``image_id`` -
                an int to set the cover photo, or null/absent to clear it.
            pin_slug: Slug of the pin to update; must belong to the requester.

        Returns:
            JSON ``{"cover_photo": null}`` when cleared, or
            ``{"cover_photo": <url>}`` with the new cover's image URL (the
            lightbox uses this to update the page live without a reload).

        Raises:
            Http404: The pin doesn't exist/belong to the requester, or the
                given image isn't eligible (not tied to this pin or its
                Location).
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        try:
            data = json.loads(request.body)
            image_id = data.get("image_id")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request data."}, status=400)

        if image_id is None:
            pin.cover_photo = None
            pin.save(update_fields=["cover_photo", "updated"])
            return JsonResponse({"cover_photo": None})

        # Any image tied to this pin's own gallery, or already associated
        # with its Location (e.g. a Media-gallery item materialized via
        # "send to wiki" or a prior cover-photo pick), is eligible.
        image = get_object_or_404(Image, pk=image_id)
        if image.pin_id != pin.pk and image.location_id != pin.location_id:
            raise Http404
        pin.cover_photo = image
        pin.save(update_fields=["cover_photo", "updated"])
        return JsonResponse({"cover_photo": image.image.url if image.image else image.source_url})


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
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).order_by("-created")
        page_obj = get_page(request, images, _GALLERY_PAGE_SIZE)
        return {"location": location, "wiki": wiki, "images": page_obj.object_list, "page_obj": page_obj, "profile": profile, "context_type": "wiki"}

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        ctx = self._get_context(request, location_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        """Upload an image to a location wiki. Rejects a file the uploader already has on this wiki."""
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(wiki=wiki, profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo to this wiki."}, status=409)
        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        img = Image.objects.create(
            image=image_file,
            wiki=wiki,
            location=location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            checksum=checksum,
            file_size=image_file.size,
        )
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class WikiGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the wiki photo map layer."""

    def get(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).with_coords()
        data = [image_to_gallery_json(img, request, profile) for img in images]
        return JsonResponse({"images": data})


class WikiCoverPhotoView(LoginRequiredMixin, View):
    """Set or clear a wiki's hero-banner cover photo.

    Any profile with a pin at the wiki's location may set it - the wiki is
    community content editable by everyone who can see it (comments, markup,
    aliases share the same access rule; see ``resolve_visible_wiki``). Each
    viewer's own ``show_wiki_cover_photos`` preference (see the pin detail
    template) independently controls whether they see it.
    """

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        try:
            data = json.loads(request.body)
            image_id = data.get("image_id")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request data."}, status=400)

        if image_id is None:
            wiki.cover_photo = None
            wiki.save(update_fields=["cover_photo", "updated"])
            return JsonResponse({"cover_photo": None})

        image = get_object_or_404(Image, pk=image_id)
        if image.wiki_id != wiki.pk and image.location_id != location.pk:
            raise Http404
        wiki.cover_photo = image
        wiki.save(update_fields=["cover_photo", "updated"])
        return JsonResponse({"cover_photo": image.image.url if image.image else image.source_url})


class WikiImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a wiki."""

    def _get_image(self, request: HttpRequest, image_id: int, location_slug: str) -> tuple[Image, Profile]:
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        return get_object_or_404(Image, pk=image_id, wiki=wiki), profile

    def post(self, request: HttpRequest, location_slug: str, image_id: int) -> JsonResponse:
        img, profile = self._get_image(request, image_id, location_slug)
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
        img, profile = self._get_image(request, image_id, location_slug)
        if img.profile != profile:
            raise Http404
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)
