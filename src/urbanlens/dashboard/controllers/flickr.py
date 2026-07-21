"""Flickr integration controller.

Three groups of views:

- Settings ("Connect Flickr"): ``FlickrSettingsView`` (read-only subsection
  partial), ``FlickrConnectView``/``FlickrCallbackView`` (OAuth 1.0a 3-legged
  flow), ``FlickrDisconnectView``.
- Pin detail ("Import from Flickr"): server-side geo search over *one user's
  own* OAuth-connected library (no thumbnail proxy needed - Flickr's photo
  URLs are public, capability-scoped per photo) and a Celery-backed import
  with progress polling.
- Pin/wiki Media ("Import a Flickr Album"): given the public URL of *any*
  Flickr user's public album/photoset (no OAuth involved - see
  ``services.apis.flickr.public``), preview its photos and import selected
  ones. Same picker + Celery-progress-polling shape as the section above,
  parameterized over a pin or a wiki target.
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
from urbanlens.dashboard.services.apis.flickr.oauth import FlickrNotConfiguredError, finish_authorization, is_configured as flickr_is_configured, start_authorization
from urbanlens.dashboard.services.apis.flickr.public import MAX_ALBUM_PHOTOS, FlickrPublicGateway
from urbanlens.dashboard.services.celery import get_task_progress, safely_enqueue_task
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.photo_import import PhotoImportMode, visit_dates_for_pin
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SETTINGS_PARTIAL = "dashboard/partials/settings/_flickr_account.html"
_PICKER_PARTIAL = "dashboard/partials/pins/_flickr_picker_dialog.html"
_PROGRESS_PARTIAL = "dashboard/partials/pins/_flickr_import_progress.html"
_ALBUM_DIALOG_PARTIAL = "dashboard/partials/pins/_flickr_album_dialog.html"
_ALBUM_PROGRESS_PARTIAL = "dashboard/partials/pins/_flickr_album_import_progress.html"
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


# -- Public Flickr album import (pin + wiki) ----------------------------------
#
# Shared logic lives in the module-level helpers below; each pin/wiki View
# pair is a thin wrapper supplying its own target resolution + URL names
# (mirroring how PinGalleryView/WikiGalleryView share _photo_gallery.html but
# differ in permission checks and which FK gets set).


def _album_base_context(*, target_kind: str, lookup_url: str, import_url: str) -> dict:
    return {
        "target_kind": target_kind,
        "lookup_url": lookup_url,
        "import_url": import_url,
        "flickr_configured": flickr_is_configured(),
        "max_photos": MAX_ALBUM_PHOTOS,
    }


def _album_lookup_response(request: HttpRequest, *, dedupe_urls: set[str], context: dict) -> HttpResponse:
    """Shared POST handler: resolve a pasted album URL into a preview grid.

    Args:
        request: The POST request carrying ``album_url``.
        dedupe_urls: The target's existing ``Image.source_url`` values, so
            already-imported photos can be flagged/disabled in the grid.
        context: The target-specific base context (URLs, target_kind, etc.).

    Returns:
        The rendered dialog body - either an error, or the preview grid.
    """
    album_url = (request.POST.get("album_url") or "").strip()
    if not album_url:
        return render(request, _ALBUM_DIALOG_PARTIAL, {**context, "error": "Paste a Flickr album URL first."})
    if not flickr_is_configured():
        return render(request, _ALBUM_DIALOG_PARTIAL, {**context, "error": "Flickr integration is not configured on this server."})

    from urbanlens.dashboard.services.apis.flickr.public import photo_web_url

    try:
        album = FlickrPublicGateway().get_album(album_url)
    except (ValueError, GatewayRequestError) as exc:
        return render(request, _ALBUM_DIALOG_PARTIAL, {**context, "error": str(exc)})

    assets = [
        {"id": photo.id, "thumbnail_url": photo.thumbnail_url, "already_imported": photo_web_url(album.owner_nsid, photo.id) in dedupe_urls}
        for photo in album.photos
    ]
    return render(request, _ALBUM_DIALOG_PARTIAL, {**context, "album": album, "album_url": album_url, "assets": assets})


def _album_import_response(request: HttpRequest, *, target_kind: str, target_id: int, profile: Profile, album_url: str, photo_ids: list[str], progress_url_for: Callable[[str], str]) -> HttpResponse:
    """Shared POST handler: enqueue the Celery import task for selected photos.

    Args:
        request: The submitting POST request.
        target_kind: ``"pin"`` or ``"wiki"``.
        target_id: PK of the pin or wiki.
        profile: The requesting profile.
        album_url: The album URL submitted with the form (re-resolved inside
            the task rather than trusting a client-supplied photo list).
        photo_ids: Selected Flickr photo ids.
        progress_url_for: Builds the polling URL given a task id.

    Returns:
        The initial progress fragment, or a 503 fragment when the queue is
        unavailable.
    """
    from urbanlens.dashboard.tasks import import_flickr_album_photos

    result = safely_enqueue_task(import_flickr_album_photos, target_kind, target_id, profile.pk, album_url, photo_ids)
    if result is None:
        return render(request, _ALBUM_PROGRESS_PARTIAL, {"state": "FAILURE", "message": "Import queue is unavailable. Please try again later."}, status=503)
    return render(request, _ALBUM_PROGRESS_PARTIAL, {"progress_url": progress_url_for(result.id), "state": "PENDING", "percent": 0, "message": "Starting import..."})


def _album_progress_response(request: HttpRequest, *, task_id: str, progress_url: str) -> HttpResponse:
    """Shared GET handler: poll a Celery task's progress and render the fragment.

    Args:
        request: The polling GET request (used only for ``render``'s context).
        task_id: The Celery task id being polled.
        progress_url: This same view's own URL (for the fragment's next poll).

    Returns:
        The progress fragment, with an ``HX-Trigger`` toast + gallery refresh
        once the task settles.
    """
    progress = get_task_progress(task_id)
    context = {"progress_url": progress_url, "state": progress.state, "percent": progress.percent, "message": progress.message, "error": progress.error}
    response = render(request, _ALBUM_PROGRESS_PARTIAL, context)
    if progress.state == "SUCCESS":
        result = progress.result or {}
        summary = f"Imported {result.get('imported', 0)} photo(s)" + (f", skipped {result.get('skipped')} duplicate(s)" if result.get("skipped") else "") + "."
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": summary}, "refreshGallery": {}})
    elif progress.state in {"FAILURE", "REVOKED"}:
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": progress.error or "Import failed."}})
    return response


class PinFlickrAlbumDialogView(LoginRequiredMixin, View):
    """GET pin/<slug>/flickr-album/ - the initial "paste an album URL" dialog body."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        context = _album_base_context(
            target_kind="pin",
            lookup_url=reverse("pin.flickr_album.lookup", args=[pin.slug]),
            import_url=reverse("pin.flickr_album.import", args=[pin.slug]),
        )
        return render(request, _ALBUM_DIALOG_PARTIAL, context)


class PinFlickrAlbumLookupView(LoginRequiredMixin, View):
    """POST pin/<slug>/flickr-album/lookup/ - resolve the URL and preview its photos."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        dedupe_urls = set(Image.objects.filter(pin=pin, profile=profile, source_url__isnull=False).values_list("source_url", flat=True))
        context = _album_base_context(
            target_kind="pin",
            lookup_url=reverse("pin.flickr_album.lookup", args=[pin.slug]),
            import_url=reverse("pin.flickr_album.import", args=[pin.slug]),
        )
        return _album_lookup_response(request, dedupe_urls=dedupe_urls, context=context)


class PinFlickrAlbumImportView(LoginRequiredMixin, View):
    """POST pin/<slug>/flickr-album/import/ - enqueue import of the selected photos."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        album_url = (request.POST.get("album_url") or "").strip()
        photo_ids = request.POST.getlist("photo_ids")
        if not photo_ids:
            return HttpResponse('<p class="immich-picker-error">Select at least one photo to import.</p>', status=400)
        return _album_import_response(
            request,
            target_kind="pin",
            target_id=pin.pk,
            profile=profile,
            album_url=album_url,
            photo_ids=photo_ids,
            progress_url_for=lambda task_id: reverse("pin.flickr_album.import.progress", args=[pin.slug, task_id]),
        )


class PinFlickrAlbumImportProgressView(LoginRequiredMixin, View):
    """GET pin/<slug>/flickr-album/import/<task_id>/progress/ - polled progress fragment."""

    def get(self, request: HttpRequest, pin_slug: str, task_id: str) -> HttpResponse:
        get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return _album_progress_response(request, task_id=task_id, progress_url=reverse("pin.flickr_album.import.progress", args=[pin_slug, task_id]))


class WikiFlickrAlbumDialogView(LoginRequiredMixin, View):
    """GET location/<slug>/wiki/flickr-album/ - the initial dialog body."""

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        context = _album_base_context(
            target_kind="wiki",
            lookup_url=reverse("location.wiki.flickr_album.lookup", args=[location.slug]),
            import_url=reverse("location.wiki.flickr_album.import", args=[location.slug]),
        )
        return render(request, _ALBUM_DIALOG_PARTIAL, context)


class WikiFlickrAlbumLookupView(LoginRequiredMixin, View):
    """POST location/<slug>/wiki/flickr-album/lookup/ - resolve the URL and preview its photos."""

    def post(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        dedupe_urls = set(Image.objects.filter(wiki=wiki, profile=profile, source_url__isnull=False).values_list("source_url", flat=True))
        context = _album_base_context(
            target_kind="wiki",
            lookup_url=reverse("location.wiki.flickr_album.lookup", args=[location.slug]),
            import_url=reverse("location.wiki.flickr_album.import", args=[location.slug]),
        )
        return _album_lookup_response(request, dedupe_urls=dedupe_urls, context=context)


class WikiFlickrAlbumImportView(LoginRequiredMixin, View):
    """POST location/<slug>/wiki/flickr-album/import/ - enqueue import of the selected photos."""

    def post(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        album_url = (request.POST.get("album_url") or "").strip()
        photo_ids = request.POST.getlist("photo_ids")
        if not photo_ids:
            return HttpResponse('<p class="immich-picker-error">Select at least one photo to import.</p>', status=400)
        return _album_import_response(
            request,
            target_kind="wiki",
            target_id=wiki.pk,
            profile=profile,
            album_url=album_url,
            photo_ids=photo_ids,
            progress_url_for=lambda task_id: reverse("location.wiki.flickr_album.import.progress", args=[location.slug, task_id]),
        )


class WikiFlickrAlbumImportProgressView(LoginRequiredMixin, View):
    """GET location/<slug>/wiki/flickr-album/import/<task_id>/progress/ - polled progress fragment."""

    def get(self, request: HttpRequest, location_slug: str, task_id: str) -> HttpResponse:
        resolve_visible_wiki(request, location_slug)
        return _album_progress_response(request, task_id=task_id, progress_url=reverse("location.wiki.flickr_album.import.progress", args=[location_slug, task_id]))
