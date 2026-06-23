"""Image gallery controller - upload, list, reposition, and delete photos."""

from __future__ import annotations

import contextlib
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

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


def _extract_gps_coords(image_file) -> tuple[float, float] | None:
    """Return (latitude, longitude) from EXIF GPS tags, or None if not present."""
    try:
        from PIL import Image as PILImage
        from PIL.ExifTags import GPSTAGS

        image_file.seek(0)
        img = PILImage.open(image_file)
        exif = img.getexif()
        if not exif:
            return None
        gps_ifd = exif.get_ifd(0x8825)  # 34853 - GPSInfo IFD tag
        if not gps_ifd:
            return None
        gps_data = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
        if "GPSLatitude" not in gps_data or "GPSLongitude" not in gps_data:
            return None
        lat = _dms_to_decimal(gps_data["GPSLatitude"], gps_data.get("GPSLatitudeRef", "N"))
        lng = _dms_to_decimal(gps_data["GPSLongitude"], gps_data.get("GPSLongitudeRef", "E"))
        return lat, lng
    except Exception as exc:
        logger.debug("EXIF GPS extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)


def _dms_to_decimal(dms, ref: str) -> float:
    """Convert a DMS tuple from EXIF to a signed decimal degree."""
    degrees, minutes, seconds = (float(x) for x in dms)
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in {"S", "W"}:
        decimal = -decimal
    return decimal


def _image_to_json(img: Image, request: HttpRequest, viewer_profile: Profile | None = None) -> dict:
    """Serialise an Image to a dict suitable for the map photo layer."""
    return {
        "id": img.pk,
        "url": request.build_absolute_uri(img.image.url),
        "caption": img.caption or "",
        "latitude": float(img.latitude) if img.latitude is not None else None,
        "longitude": float(img.longitude) if img.longitude is not None else None,
        "uploader": img.profile.username if img.profile else "",
        "is_mine": viewer_profile is not None and img.profile_id == viewer_profile.pk,
    }


# ── Pin gallery ──────────────────────────────────────────────────────────────


class PinGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the pin detail page (loaded via HTMX)."""

    def _get_context(self, request: HttpRequest, pin_slug: str) -> dict:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(pin=pin).select_related("profile").visible_to(profile)
        return {"pin": pin, "images": images, "profile": profile, "context_type": "pin"}

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        ctx = self._get_context(request, pin_slug)
        return render(request, "dashboard/partials/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Upload an image to a pin."""
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        coords = _extract_gps_coords(image_file)
        lat = coords[0] if coords else None
        lng = coords[1] if coords else None

        img = Image.objects.create(
            image=image_file,
            pin=pin,
            location=pin.location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            latitude=Decimal(str(lat)) if lat is not None else None,
            longitude=Decimal(str(lng)) if lng is not None else None,
        )
        return JsonResponse(_image_to_json(img, request, profile), status=201)


class PinGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the pin photo map layer."""

    def get(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(pin=pin).select_related("profile").visible_to(profile).with_coords()
        data = [_image_to_json(img, request, profile) for img in images]
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
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, pin_slug: str, image_id: int) -> HttpResponse:
        img = self._get_image(image_id, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)


# ── Location wiki gallery ─────────────────────────────────────────────────────


class WikiGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the location wiki page."""

    def _get_context(self, request: HttpRequest, location_slug: str) -> dict:
        location = get_object_or_404(Location, slug=location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(location=location).select_related("profile").visible_to(profile)
        return {"location": location, "images": images, "profile": profile, "context_type": "wiki"}

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        ctx = self._get_context(request, location_slug)
        return render(request, "dashboard/partials/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        location = get_object_or_404(Location, slug=location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        coords = _extract_gps_coords(image_file)
        lat = coords[0] if coords else None
        lng = coords[1] if coords else None

        img = Image.objects.create(
            image=image_file,
            location=location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            latitude=Decimal(str(lat)) if lat is not None else None,
            longitude=Decimal(str(lng)) if lng is not None else None,
        )
        return JsonResponse(_image_to_json(img, request, profile), status=201)


class WikiGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the wiki photo map layer."""

    def get(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        location = get_object_or_404(Location, slug=location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images = Image.objects.filter(location=location).select_related("profile").visible_to(profile).with_coords()
        data = [_image_to_json(img, request, profile) for img in images]
        return JsonResponse({"images": data})


class WikiImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a location wiki."""

    def _get_image(self, image_id: int, location_slug: str) -> Image:
        return get_object_or_404(Image, pk=image_id, location__slug=location_slug)

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
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, location_slug: str, image_id: int) -> HttpResponse:
        img = self._get_image(image_id, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)
