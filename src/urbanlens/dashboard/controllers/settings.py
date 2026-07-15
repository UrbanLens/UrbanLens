"""User settings controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.forms.settings_form import (
    AISettingsForm,
    CommunitySettingsForm,
    ContactSettingsForm,
    DirectMessageSettingsForm,
    ExternalApiSettingsForm,
    HistorySettingsForm,
    KeywordTaggingSettingsForm,
    MapCenterForm,
    MapDisplayForm,
    MarkupDefaultsForm,
    PlacesLayerForm,
    PrivacySettingsForm,
    StyleSettingsForm,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature
from urbanlens.dashboard.services.apis.flickr.oauth import is_configured as flickr_is_configured
from urbanlens.dashboard.services.storage import allowed_user_dimension_values, allowed_user_video_height_values, get_storage_settings_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _settings_redirect(anchor: str) -> HttpResponse:
    """Redirect to the settings page, landing on the tab containing ``anchor``.

    The page's tab-switching JS resolves an id fragment to its containing
    ``.settings-tab-panel`` and activates that tab, so a plain section id is
    enough to land the user back where they were instead of the default tab.

    Args:
        anchor: The id of the section/subsection element to land on.

    Returns:
        A redirect response to ``settings.view#<anchor>``.
    """
    return redirect(f"{reverse('settings.view')}#{anchor}")


def _name_source_order(profile: Profile) -> list[tuple[str, str, bool]]:
    """Build the (slug, label, ranked) rows for this profile's name-source priority picker.

    Starts from the profile's own override when configured, otherwise from the
    current site-wide default, so the picker always shows a sensible starting
    point to customize rather than an arbitrary/empty list.

    Args:
        profile: The profile whose picker state to build.

    Returns:
        Ranked rows first (in priority order), then any remaining available
        sources, unranked.
    """
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.plugins.registry import plugin_registry

    priority_slugs = profile.name_source_priority_list or SiteSettings.get_current().name_source_priority_list
    providers_by_slug = {provider.source: provider.verbose_name for provider in plugin_registry.name_providers()}
    name_source_order = [(slug, providers_by_slug[slug], True) for slug in priority_slugs if slug in providers_by_slug]
    ranked_slugs = {slug for slug, _label, _ranked in name_source_order}
    name_source_order += [(slug, label, False) for slug, label in providers_by_slug.items() if slug not in ranked_slugs]
    return name_source_order


def _e2ee_enrolled(profile: Profile) -> bool:
    """Return True when the profile has a direct-message encryption key bundle.

    Args:
        profile: The profile whose enrollment to check.

    Returns:
        True when a ``MessagingKeyBundle`` exists for this profile.
    """
    from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

    return MessagingKeyBundle.objects.filter(profile=profile).exists()


def _security_context(user: User, request: HttpRequest) -> dict:
    """Context for the Security section: passkeys, TOTP status, backup codes.

    Thin wrapper around ``services.two_factor.security_settings_context``,
    which is also called directly by the 2FA action views (``two_factor.py``)
    so they can re-render just this section for htmx requests.
    """
    from urbanlens.dashboard.services.two_factor import security_settings_context

    return security_settings_context(user, request)


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
        if not isinstance(request.user, User):
            return redirect("login")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = {
            "flickr_configured": flickr_is_configured(),
            "privacy_form": PrivacySettingsForm(instance=profile),
            "contact_form": ContactSettingsForm(initial={"email": request.user.email}, exclude_user_id=request.user.pk),
            "style_form": StyleSettingsForm(instance=profile),
            "map_display_form": MapDisplayForm(instance=profile),
            "map_center_form": MapCenterForm(instance=profile),
            "places_layer_form": PlacesLayerForm(instance=profile),
            "markup_defaults_form": MarkupDefaultsForm(instance=profile),
            "ai_form": AISettingsForm(instance=profile),
            "keyword_tagging_form": KeywordTaggingSettingsForm(instance=profile),
            "history_form": HistorySettingsForm(instance=profile),
            "community_form": CommunitySettingsForm(instance=profile),
            "external_api_form": ExternalApiSettingsForm(instance=profile),
            "direct_message_form": DirectMessageSettingsForm(instance=profile),
            "name_source_order": _name_source_order(profile),
            "preview_zoom": profile.map_default_zoom or 13,
            "e2ee_enrolled": _e2ee_enrolled(profile),
            "e2ee_has_password": request.user.has_usable_password(),
            "self_slug": profile.ensure_slug(),
            **_security_context(request.user, request),
            **self._build_map_center_context(profile),
            **get_storage_settings_context(profile),
        }
        return render(request, "dashboard/pages/settings/index.html", context)

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        section = request.POST.get("section")
        # The settings page autosaves via fetch() and only checks the response
        # status - a validation failure previously fell through to the normal
        # 200 full-page re-render below (or a 302-then-200 redirect for the
        # messages.error() branches), which fetch() can't distinguish from
        # success, so the UI showed "Saved" for changes that were never
        # persisted. AJAX requests get a JSON verdict instead.
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        privacy_form = PrivacySettingsForm(instance=profile)
        contact_form = ContactSettingsForm(initial={"email": request.user.email}, exclude_user_id=request.user.pk)
        style_form = StyleSettingsForm(instance=profile)
        map_display_form = MapDisplayForm(instance=profile)
        map_center_form = MapCenterForm(instance=profile)
        places_layer_form = PlacesLayerForm(instance=profile)
        markup_defaults_form = MarkupDefaultsForm(instance=profile)
        ai_form = AISettingsForm(instance=profile)
        keyword_tagging_form = KeywordTaggingSettingsForm(instance=profile)
        history_form = HistorySettingsForm(instance=profile)
        community_form = CommunitySettingsForm(instance=profile)
        external_api_form = ExternalApiSettingsForm(instance=profile)
        direct_message_form = DirectMessageSettingsForm(instance=profile)

        if section == "places_layer":
            if user_has_feature(request.user, SiteFeature.PLACES):
                places_layer_form = PlacesLayerForm(request.POST, instance=profile)
                if places_layer_form.is_valid():
                    places_layer_form.save()
                    messages.success(request, "Places layer sources saved.")
                    return _settings_redirect("places-layer-settings-section")

        elif section == "ai":
            if user_has_feature(request.user, SiteFeature.AI):
                ai_form = AISettingsForm(request.POST, instance=profile)
                if ai_form.is_valid():
                    ai_form.save()
                    messages.success(request, "AI settings saved.")
                    return _settings_redirect("ai-settings-section")

        elif section == "keyword_tagging":
            keyword_tagging_form = KeywordTaggingSettingsForm(request.POST, instance=profile)
            if keyword_tagging_form.is_valid():
                keyword_tagging_form.save()
                messages.success(request, "Keyword tagging settings saved.")
                return _settings_redirect("keyword-tagging-settings-section")

        elif section == "markup_defaults":
            markup_defaults_form = MarkupDefaultsForm(request.POST, instance=profile)
            if markup_defaults_form.is_valid():
                markup_defaults_form.save()
                messages.success(request, "Annotation defaults saved.")
                return _settings_redirect("markup-defaults-settings-section")

        elif section == "privacy":
            privacy_form = PrivacySettingsForm(request.POST, instance=profile)
            if privacy_form.is_valid():
                privacy_form.save()
                messages.success(request, "Privacy settings saved.")
                return _settings_redirect("privacy-settings-section")

        elif section == "contact":
            contact_form = ContactSettingsForm(request.POST, exclude_user_id=request.user.pk)
            if contact_form.is_valid():
                request.user.email = contact_form.cleaned_data["email"]
                request.user.save(update_fields=["email"])
                messages.success(request, "Email address saved.")
                return _settings_redirect("notifications-settings-section")

        elif section == "style":
            style_form = StyleSettingsForm(request.POST, instance=profile)
            if style_form.is_valid():
                style_form.save()
                messages.success(request, "Style settings saved.")
                return _settings_redirect("style-settings-section")

        elif section == "storage":
            raw_dimension = (request.POST.get("image_downscale_max_dimension") or "").strip()
            if raw_dimension == "":
                profile.image_downscale_max_dimension = None
            else:
                try:
                    dimension = int(raw_dimension)
                except (ValueError, TypeError):
                    dimension = None
                if dimension is None or dimension not in allowed_user_dimension_values(profile):
                    if is_xhr:
                        return JsonResponse({"ok": False, "errors": {"image_downscale_max_dimension": ["That photo size is not available."]}})
                    messages.error(request, "That photo size is not available.")
                    return _settings_redirect("storage-settings-section")
                profile.image_downscale_max_dimension = dimension

            raw_video_height = (request.POST.get("video_downscale_max_height") or "").strip()
            if raw_video_height == "":
                profile.video_downscale_max_height = None
            else:
                try:
                    video_height = int(raw_video_height)
                except (ValueError, TypeError):
                    video_height = None
                if video_height is None or video_height not in allowed_user_video_height_values(profile):
                    if is_xhr:
                        return JsonResponse({"ok": False, "errors": {"video_downscale_max_height": ["That video quality is not available."]}})
                    messages.error(request, "That video quality is not available.")
                    return _settings_redirect("storage-settings-section")
                profile.video_downscale_max_height = video_height

            profile.save(update_fields=["image_downscale_max_dimension", "video_downscale_max_height", "updated"])
            messages.success(request, "Storage settings saved. The new photo/video quality applies to future uploads.")
            return _settings_redirect("storage-settings-section")

        elif section == "map":
            map_display_form = MapDisplayForm(request.POST, instance=profile)
            map_center_form = MapCenterForm(request.POST, instance=profile)
            if map_display_form.is_valid() and map_center_form.is_valid():
                map_display_form.save()
                map_center_form.save()
                messages.success(request, "Map settings saved.")
                return _settings_redirect("map-settings-section")

        elif section == "history":
            history_form = HistorySettingsForm(request.POST, instance=profile)
            if history_form.is_valid():
                history_form.save()
                messages.success(request, "History settings saved.")
                return _settings_redirect("history-settings-section")

        elif section == "community":
            community_form = CommunitySettingsForm(request.POST, instance=profile)
            if community_form.is_valid():
                community_form.save()
                messages.success(request, "Community settings saved.")
                return _settings_redirect("community-settings-section")

        elif section == "external_apis":
            external_api_form = ExternalApiSettingsForm(request.POST, instance=profile)
            if external_api_form.is_valid():
                external_api_form.save()
                messages.success(request, "External API settings saved.")
                return _settings_redirect("external-api-settings-section")

        elif section == "direct_messages":
            direct_message_form = DirectMessageSettingsForm(request.POST, instance=profile)
            if direct_message_form.is_valid():
                direct_message_form.save()
                messages.success(request, "Direct message settings saved.")
                return _settings_redirect("direct-message-settings-section")

        context = {
            "flickr_configured": flickr_is_configured(),
            "privacy_form": privacy_form,
            "contact_form": contact_form,
            "style_form": style_form,
            "map_display_form": map_display_form,
            "map_center_form": map_center_form,
            "places_layer_form": places_layer_form,
            "markup_defaults_form": markup_defaults_form,
            "ai_form": ai_form,
            "keyword_tagging_form": keyword_tagging_form,
            "history_form": history_form,
            "community_form": community_form,
            "external_api_form": external_api_form,
            "direct_message_form": direct_message_form,
            "name_source_order": _name_source_order(profile),
            "preview_zoom": profile.map_default_zoom or 13,
            **_security_context(request.user, request),
            **self._build_map_center_context(profile),
            **get_storage_settings_context(profile),
        }
        if is_xhr:
            # Exactly one of these is bound-and-invalid (whichever `section` matched
            # above and failed `is_valid()`) - the rest were never bound with POST
            # data, so their .errors are empty.
            bound_forms = (
                privacy_form,
                contact_form,
                style_form,
                map_display_form,
                map_center_form,
                places_layer_form,
                markup_defaults_form,
                ai_form,
                keyword_tagging_form,
                history_form,
                community_form,
                external_api_form,
                direct_message_form,
            )
            errors: dict[str, object] = {}
            for form in bound_forms:
                if form.errors:
                    errors.update(form.errors)
            return JsonResponse({"ok": False, "errors": errors})
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

    if request.user.is_authenticated:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.external_apis_enabled:
            return JsonResponse({"error": "External lookups are turned off in your settings."}, status=403)

    # Try Google Geocoding.
    try:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

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
