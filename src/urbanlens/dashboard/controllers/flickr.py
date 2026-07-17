"""Flickr integration controller.

Two groups of views:

- Settings ("Connect Flickr"): ``FlickrSettingsView`` (read-only subsection
  partial), ``FlickrConnectView``/``FlickrCallbackView`` (OAuth 1.0a 3-legged
  flow), ``FlickrDisconnectView``.
- Pin detail ("Import from Flickr"): server-side geo search (no thumbnail
  proxy needed - Flickr's photo URLs are public, capability-scoped per photo)
  and a Celery-backed import with progress polling.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, _haversine_km
from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway, FlickrPhoto
from urbanlens.dashboard.services.apis.flickr.oauth import FlickrNotConfiguredError, finish_authorization, start_authorization
from urbanlens.dashboard.services.celery import get_task_progress, safely_enqueue_task
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.photo_import import PhotoImportMode, visit_dates_for_pin

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SETTINGS_PARTIAL = "dashboard/partials/settings/_flickr_account.html"
_PICKER_PARTIAL = "dashboard/partials/pins/_flickr_picker_dialog.html"
_PROGRESS_PARTIAL = "dashboard/partials/pins/_flickr_import_progress.html"
_RADIUS_CHOICES_M = ((100, "100 m"), (250, "250 m"), (500, "500 m"), (1000, "1 km"), (2000, "2 km"), (5000, "5 km"))
_DEFAULT_RADIUS_M = 500
_REQUEST_TOKEN_CACHE_TTL = 600
_EMPTY_MESSAGES: dict[str, str] = {
    PhotoImportMode.NEARBY: "No photos found within that distance.",
    PhotoImportMode.VISITS: "No photos found on your recorded visit dates.",
    PhotoImportMode.ALL: "No photos found in your library.",
}


def _request_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _with_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


def _request_token_cache_key(oauth_token: str) -> str:
    return f"ul_flickr_request_token_{oauth_token}"


def _within_radius(pin_point: tuple[float, float], photo: FlickrPhoto, radius_m: int) -> bool:
    """Whether a photo is within ``radius_m`` of the pin, re-checking locally.

    Flickr's search already filters server-side, so this only re-verifies
    photos that reported coordinates - one with none is kept as-is (Flickr
    included it in the radius search results, so it's trusted).

    Args:
        pin_point: (latitude, longitude) of the pin.
        photo: The candidate photo.
        radius_m: The search radius in meters.

    Returns:
        True when the photo should be kept.
    """
    if photo.lat is None or photo.lon is None:
        return True
    return _haversine_km(pin_point, (photo.lat, photo.lon)) * 1000 <= radius_m


# -- Settings: connect / disconnect -------------------------------------------


class FlickrSettingsView(LoginRequiredMixin, View):
    """GET /settings/flickr/ - HTMX subsection showing the current connection state."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        account = FlickrAccount.objects.get_for_profile(profile)
        return render(request, _SETTINGS_PARTIAL, {"account": account})


class FlickrConnectView(LoginRequiredMixin, View):
    """GET /settings/flickr/connect/ - start the OAuth1 flow and redirect to Flickr."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        callback_uri = request.build_absolute_uri(reverse("settings.flickr.callback"))
        try:
            pending = start_authorization(callback_uri)
        except (FlickrNotConfiguredError, GatewayRequestError):
            messages.error(request, "Flickr integration is not configured on this server.")
            return redirect(f"{reverse('settings.view')}#flickr-settings-section")

        cache.set(_request_token_cache_key(pending.oauth_token), {"secret": pending.oauth_token_secret, "pid": profile.id}, _REQUEST_TOKEN_CACHE_TTL)
        return redirect(pending.authorization_url)


class FlickrCallbackView(LoginRequiredMixin, View):
    """GET /settings/flickr/callback/ - exchange the verifier and store the access token."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        oauth_token = request.GET.get("oauth_token") or ""
        oauth_verifier = request.GET.get("oauth_verifier") or ""
        stashed = cache.get(_request_token_cache_key(oauth_token)) if oauth_token else None
        if not stashed or not oauth_verifier or stashed.get("pid") != profile.id:
            messages.error(request, "The Flickr connection request was invalid or expired. Please try again.")
            return redirect(f"{reverse('settings.view')}#flickr-settings-section")
        cache.delete(_request_token_cache_key(oauth_token))

        try:
            grant = finish_authorization(oauth_token=oauth_token, oauth_token_secret=stashed["secret"], oauth_verifier=oauth_verifier)
        except (FlickrNotConfiguredError, GatewayRequestError):
            messages.error(request, "Flickr access was not granted.")
            return redirect(f"{reverse('settings.view')}#flickr-settings-section")

        FlickrAccount.objects.update_or_create(
            profile=profile,
            defaults={
                "oauth_token": grant.oauth_token,
                "oauth_token_secret": grant.oauth_token_secret,
                "flickr_user_id": grant.user_nsid,
                "flickr_username": grant.username,
            },
        )
        messages.success(request, "Flickr connected.")
        return redirect("settings.view")


class FlickrDisconnectView(LoginRequiredMixin, View):
    """POST /settings/flickr/disconnect/ - remove the stored Flickr connection."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        FlickrAccount.objects.delete_for_profile(profile)
        response = render(request, _SETTINGS_PARTIAL, {"account": None})
        return _with_toast(response, "Flickr disconnected.")


# -- Pin detail: search / import ----------------------------------------------


class PinFlickrSearchView(LoginRequiredMixin, View):
    """GET pin/<slug>/flickr/search/ - the user's own photos, filtered by mode.

    Three modes (see ``PhotoImportMode``): nearby this pin's location, taken on
    one of the pin's recorded PinVisit dates, or unfiltered (most recent first).
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        account = FlickrAccount.objects.get_for_profile(profile)
        mode = request.GET.get("mode", PhotoImportMode.NEARBY)
        if mode not in PhotoImportMode.values:
            mode = PhotoImportMode.NEARBY
        try:
            radius_m = int(request.GET.get("radius_m", _DEFAULT_RADIUS_M))
        except (TypeError, ValueError):
            radius_m = _DEFAULT_RADIUS_M
        valid_radii = {value for value, _label in _RADIUS_CHOICES_M}
        if radius_m not in valid_radii:
            radius_m = _DEFAULT_RADIUS_M

        context = {
            "pin": pin,
            "account": account,
            "mode": mode,
            "mode_choices": PhotoImportMode.choices,
            "radius_m": radius_m,
            "radius_choices_m": _RADIUS_CHOICES_M,
        }
        if account is None:
            return render(request, _PICKER_PARTIAL, context)
        if not profile.external_apis_enabled:
            return render(request, _PICKER_PARTIAL, {**context, "error": "External lookups are turned off in your settings."})

        gateway = FlickrGateway(account=account)
        try:
            if mode == PhotoImportMode.VISITS:
                dates = visit_dates_for_pin(pin)
                if not dates:
                    return render(request, _PICKER_PARTIAL, {**context, "assets": [], "empty_message": "No recorded visits for this pin yet."})
                photos = gateway.search_by_dates(dates)
            elif mode == PhotoImportMode.ALL:
                photos = gateway.list_recent()
            else:
                if pin.location is None or pin.location.latitude is None or pin.location.longitude is None:
                    return render(request, _PICKER_PARTIAL, {**context, "error": "This pin has no location to search near."})
                photos = gateway.search_near(float(pin.location.latitude), float(pin.location.longitude), radius_m / 1000)
                # Flickr's radius search is already server-side; re-check distance
                # locally only for photos that reported coordinates (some may not),
                # matching the search's own radius rather than trusting it blindly.
                pin_point = (float(pin.location.latitude), float(pin.location.longitude))
                photos = [photo for photo in photos if _within_radius(pin_point, photo, radius_m)]
        except GatewayRequestError as exc:
            return render(request, _PICKER_PARTIAL, {**context, "error": str(exc)})

        already_imported = set(Image.objects.filter(pin=pin, profile=profile, source_url__isnull=False).values_list("source_url", flat=True))
        assets = [{"id": photo.id, "thumbnail_url": photo.thumbnail_url, "already_imported": account.photo_web_url(photo.id) in already_imported} for photo in photos]
        return render(request, _PICKER_PARTIAL, {**context, "assets": assets, "empty_message": _EMPTY_MESSAGES[mode]})


class PinFlickrImportView(LoginRequiredMixin, View):
    """POST pin/<slug>/flickr/import/ - enqueue import of the selected photos."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _request_profile(request)
        photo_ids = request.POST.getlist("photo_ids")
        if not photo_ids:
            return HttpResponse('<p class="immich-import-error">Select at least one photo to import.</p>', status=400)
        if not FlickrAccount.objects.filter(profile=profile).exists():
            return HttpResponse('<p class="immich-import-error">Flickr is not connected.</p>', status=400)

        from urbanlens.dashboard.tasks import import_flickr_photos

        result = safely_enqueue_task(import_flickr_photos, pin.pk, profile.pk, photo_ids)
        if result is None:
            return render(request, _PROGRESS_PARTIAL, {"pin": pin, "state": "FAILURE", "message": "Import queue is unavailable. Please try again later."}, status=503)
        return render(request, _PROGRESS_PARTIAL, {"pin": pin, "task_id": result.id, "state": "PENDING", "percent": 0, "message": "Starting import..."})


class PinFlickrImportProgressView(LoginRequiredMixin, View):
    """GET pin/<slug>/flickr/import/<task_id>/progress/ - polled progress fragment."""

    def get(self, request: HttpRequest, pin_slug: str, task_id: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        progress = get_task_progress(task_id)
        context = {"pin": pin, "task_id": task_id, "state": progress.state, "percent": progress.percent, "message": progress.message, "error": progress.error}
        response = render(request, _PROGRESS_PARTIAL, context)
        if progress.state == "SUCCESS":
            result = progress.result or {}
            summary = f"Imported {result.get('imported', 0)} photo(s)" + (f", skipped {result.get('skipped')} duplicate(s)" if result.get("skipped") else "") + "."
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": summary}, "refreshGallery": {}})
        elif progress.state in {"FAILURE", "REVOKED"}:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": progress.error or "Import failed."}})
        return response
