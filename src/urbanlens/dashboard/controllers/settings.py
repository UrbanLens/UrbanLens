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
    PrivacySettingsForm,
    StyleSettingsForm,
)
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


class SettingsView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("login")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        map_center = profile.get_map_center()
        context = {
            "privacy_form": PrivacySettingsForm(instance=profile),
            "contact_form": ContactSettingsForm(initial={"email": request.user.email}),
            "style_form": StyleSettingsForm(instance=profile),
            "map_center_form": MapCenterForm(instance=profile),
            "preview_lat": map_center[0] if map_center else None,
            "preview_lng": map_center[1] if map_center else None,
            "preview_zoom": profile.map_default_zoom or 13,
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

        elif section == "map_center":
            map_center_form = MapCenterForm(request.POST, instance=profile)
            if map_center_form.is_valid():
                map_center_form.save()
                messages.success(request, "Map center saved.")
                return redirect("settings.view")

        map_center = profile.get_map_center()
        context = {
            "privacy_form": privacy_form,
            "contact_form": contact_form,
            "style_form": style_form,
            "map_center_form": map_center_form,
            "preview_lat": map_center[0] if map_center else None,
            "preview_lng": map_center[1] if map_center else None,
            "preview_zoom": profile.map_default_zoom or 13,
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
