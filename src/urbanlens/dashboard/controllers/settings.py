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
    AISettingsForm,
    ContactSettingsForm,
    MapCenterForm,
    MapDisplayForm,
    MarkupDefaultsForm,
    PrivacySettingsForm,
    StyleSettingsForm,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature

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

        Uses the cached centroid (map_center_latitude/longitude on the profile) to
        avoid the expensive O(n²) haversine computation on every page GET.  The cache
        is refreshed lazily by compute_map_center() only when it is cold (null).
        """
        from urbanlens.dashboard.models.profile.model import MapCenterMode

        pin_centroid_lat: float | None
        pin_centroid_lng: float | None
        if profile.map_center_latitude is not None and profile.map_center_longitude is not None:
            pin_centroid_lat = float(profile.map_center_latitude)
            pin_centroid_lng = float(profile.map_center_longitude)
        else:
            centroid = profile.compute_map_center()
            pin_centroid_lat = centroid[0] if centroid else None
            pin_centroid_lng = centroid[1] if centroid else None

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
            "markup_defaults_form": MarkupDefaultsForm(instance=profile),
            "ai_form": AISettingsForm(instance=profile),
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
        markup_defaults_form = MarkupDefaultsForm(instance=profile)
        ai_form = AISettingsForm(instance=profile)

        if section == "ai":

            if user_has_feature(request.user, SiteFeature.AI):
                ai_form = AISettingsForm(request.POST, instance=profile)
                if ai_form.is_valid():
                    ai_form.save()
                    messages.success(request, "AI settings saved.")
                    return redirect("settings.view")

        elif section == "markup_defaults":
            markup_defaults_form = MarkupDefaultsForm(request.POST, instance=profile)
            if markup_defaults_form.is_valid():
                markup_defaults_form.save()
                messages.success(request, "Annotation defaults saved.")
                return redirect("settings.view")

        elif section == "privacy":
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
                messages.success(request, "Email address saved.")
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
            "markup_defaults_form": markup_defaults_form,
            "ai_form": ai_form,
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

    # Try Google Geocoding.
    try:
        from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway

        gateway = GoogleGeocodingGateway()
        result = gateway.geocode_place_name(address)
        if result:
            results = result.get("results", [])
            if results:
                try:
                    loc = results[0]["geometry"]["location"]
                    return JsonResponse({"lat": loc["lat"], "lng": loc["lng"]})
                except (KeyError, TypeError):
                    logger.warning("Google geocoding returned malformed result for %r", address, exc_info=True)
            logger.warning("Google geocoding returned no results for %r (status: %s)", address, result.get("status"))
    except (ImportError, OSError, ValueError):
        logger.warning("Google geocoding unavailable for %r", address, exc_info=True)

    # Fall back to Nominatim (OpenStreetMap) - no API key required.
    try:
        from geopy.geocoders import Nominatim

        geolocator = Nominatim(user_agent="urbanlens-settings/1.0")
        location = geolocator.geocode(address, timeout=5)
        if location:
            return JsonResponse({"lat": location.latitude, "lng": location.longitude})
    except (ImportError, OSError, ValueError):
        logger.warning("Nominatim geocoding failed for %r", address, exc_info=True)

    return JsonResponse({"error": "Location not found."}, status=404)


class SaveMapDarkModeView(LoginRequiredMixin, View):
    """POST endpoint to persist the user's map dark-mode preference.

    Accepts a single ``mode`` field: 'light', 'dark', or 'system'.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        mode = request.POST.get("mode", "").strip()
        if mode not in {"light", "dark", "system"}:
            return JsonResponse({"error": "mode must be light, dark, or system"}, status=400)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        Profile.objects.filter(pk=profile.pk).update(map_dark_mode=mode)
        return JsonResponse({"ok": True, "mode": mode})


class SaveMapPositionView(LoginRequiredMixin, View):
    """POST endpoint to save the user's last map pan/zoom for REMEMBER mode.

    Accepts lat, lng (float strings) and zoom (integer string). Only writes
    to the profile when map_center_mode is 'remember'; ignores the request
    silently otherwise so stale JS calls are harmless.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.dashboard.models.profile.model import MapCenterMode

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if profile.map_center_mode != MapCenterMode.REMEMBER:
            return JsonResponse({"ok": False, "reason": "not in remember mode"})

        try:
            lat = float(request.POST["lat"])
            lng = float(request.POST["lng"])
            zoom = int(request.POST["zoom"])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({"error": "lat, lng, zoom required"}, status=400)

        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180) or not (0 <= zoom <= 22):
            return JsonResponse({"error": "out of range"}, status=400)

        Profile.objects.filter(pk=profile.pk).update(
            remembered_map_lat=lat,
            remembered_map_lng=lng,
            remembered_map_zoom=zoom,
        )
        return JsonResponse({"ok": True})
