"""User settings controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views import View

from urbanlens.dashboard.forms.settings_form import (
    ContactSettingsForm,
    MapCenterForm,
    MapDisplayForm,
    PrivacySettingsForm,
    StyleSettingsForm,
)
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


class SettingsView(LoginRequiredMixin, View):
    def _build_map_center_context(self, profile: Profile) -> dict:
        """Return preview coordinates and centroid for the map-center settings section.

        The preview differs by mode:
        - CUSTOM: show the stored custom coordinates.
        - GPS / AUTO: show the pin-cluster centroid (GPS mode adds live geolocation
          on top of this in the browser).

        We always recompute the centroid here rather than reading the cached value so
        that any stale cache entry from the old averaging algorithm is replaced with
        the current clustering result.
        """
        from urbanlens.dashboard.models.profile.model import MapCenterMode

        pin_centroid = profile.compute_map_center()
        pin_centroid_lat = pin_centroid[0] if pin_centroid else None
        pin_centroid_lng = pin_centroid[1] if pin_centroid else None

        if profile.map_center_mode == MapCenterMode.CUSTOM:
            preview_lat = float(profile.map_custom_latitude) if profile.map_custom_latitude is not None else None
            preview_lng = float(profile.map_custom_longitude) if profile.map_custom_longitude is not None else None
        else:
            preview_lat = pin_centroid_lat
            preview_lng = pin_centroid_lng

        return {
            "preview_lat": preview_lat,
            "preview_lng": preview_lng,
            "pin_centroid_lat": pin_centroid_lat,
            "pin_centroid_lng": pin_centroid_lng,
        }

    def get(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("login")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = {
            "privacy_form": PrivacySettingsForm(instance=profile),
            "contact_form": ContactSettingsForm(initial={"email": request.user.email}),
            "style_form": StyleSettingsForm(instance=profile),
            "map_display_form": MapDisplayForm(instance=profile),
            "map_center_form": MapCenterForm(instance=profile),
            "preview_zoom": profile.map_default_zoom or 13,
            **self._build_map_center_context(profile),
        }
        return render(request, "dashboard/pages/settings/index.html", context)

    def post(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("login")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        section = request.POST.get("section")

        privacy_form = PrivacySettingsForm(instance=profile)
        contact_form = ContactSettingsForm(initial={"email": request.user.email})
        style_form = StyleSettingsForm(instance=profile)
        map_display_form = MapDisplayForm(instance=profile)
        map_center_form = MapCenterForm(instance=profile)

        if section == "privacy":
            privacy_form = PrivacySettingsForm(request.POST, instance=profile)
            if privacy_form.is_valid():
                privacy_form.save()
                messages.success(request, "Privacy settings saved.")
                return redirect("settings.view")

        elif section == "contact":
            contact_form = ContactSettingsForm(request.POST)
            if contact_form.is_valid():
                request.user.email = contact_form.cleaned_data["email"]
                request.user.save(update_fields=["email"])
                messages.success(request, "Contact settings saved.")
                return redirect("settings.view")

        elif section == "style":
            style_form = StyleSettingsForm(request.POST, instance=profile)
            if style_form.is_valid():
                style_form.save()
                messages.success(request, "Style settings saved.")
                return redirect("settings.view")

        elif section == "map":
            map_display_form = MapDisplayForm(request.POST, instance=profile)
            map_center_form = MapCenterForm(request.POST, instance=profile)
            if map_display_form.is_valid() and map_center_form.is_valid():
                map_display_form.save()
                map_center_form.save()
                messages.success(request, "Map settings saved.")
                return redirect("settings.view")

        context = {
            "privacy_form": privacy_form,
            "contact_form": contact_form,
            "style_form": style_form,
            "map_display_form": map_display_form,
            "map_center_form": map_center_form,
            "preview_zoom": profile.map_default_zoom or 13,
            **self._build_map_center_context(profile),
        }
        return render(request, "dashboard/pages/settings/index.html", context)


def geocode_address(request: HttpRequest) -> JsonResponse:
    """Return lat/lng for a free-text address or 'lat,lng' string.

    Accepts:
        GET ?address=<text>

    Returns:
        JSON {lat, lng} on success, or {error} with an appropriate HTTP status.
    """
    address = request.GET.get("address", "").strip()
    if not address:
        return JsonResponse({"error": "No address provided."}, status=400)

    # Try interpreting as raw 'lat, lng' coordinates first (no API call needed).
    parts = address.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lng = float(parts[1].strip())
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return JsonResponse({"lat": lat, "lng": lng})
        except ValueError:
            pass

    # Fall back to Google Geocoding.
    try:
        from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway
        gateway = GoogleGeocodingGateway()
        result = gateway.geocode_place_name(address)
        if result and result.get("results"):
            loc = result["results"][0]["geometry"]["location"]
            return JsonResponse({"lat": loc["lat"], "lng": loc["lng"]})
    except (ValueError, KeyError):
        logger.warning("Geocoding failed for address %r", address)

    return JsonResponse({"error": "Location not found."}, status=404)
