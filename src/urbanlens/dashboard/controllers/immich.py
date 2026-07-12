"""Immich integration controller.

Two groups of views:

- Settings ("Connect Immich"): ``ImmichSettingsView`` / ``ImmichDisconnectView``,
  loaded as an HTMX subsection on the settings page (mirrors ``UndoHistoryView``).
- Pin detail ("Import from Immich"): search, a thumbnail proxy (keeps the API
  key server-side), and a Celery-backed import with progress polling.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.forms.immich_form import ImmichAccountForm
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, _haversine_km
from urbanlens.dashboard.services.apis.immich import ImmichGateway
from urbanlens.dashboard.services.celery import get_task_progress, safely_enqueue_task
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.photo_import import PhotoImportMode, visit_dates_for_pin

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SETTINGS_PARTIAL = "dashboard/partials/settings/_immich_account.html"
_PICKER_PARTIAL = "dashboard/partials/pins/_immich_picker_dialog.html"
_PROGRESS_PARTIAL = "dashboard/partials/pins/_immich_import_progress.html"
_THUMBNAIL_CACHE_TTL = 60 * 60 * 24
_RADIUS_CHOICES_M = ((100, "100 m"), (250, "250 m"), (500, "500 m"), (1000, "1 km"), (2000, "2 km"), (5000, "5 km"))
_DEFAULT_RADIUS_M = 500
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


# -- Settings: connect / disconnect -------------------------------------------


class ImmichSettingsView(LoginRequiredMixin, View):
    """GET/POST /settings/immich/ - HTMX subsection for connecting Immich."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        account = ImmichAccount.objects.filter(profile=profile).first()
        form = ImmichAccountForm(instance=account) if account is None else None
        return render(request, _SETTINGS_PARTIAL, {"account": account, "form": form})

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        form = ImmichAccountForm(request.POST)
        if not form.is_valid():
            return render(request, _SETTINGS_PARTIAL, {"account": None, "form": form})

        candidate = ImmichAccount(profile=profile, server_url=form.cleaned_data["server_url"], api_key=form.cleaned_data["api_key"])
        if not ImmichGateway(account=candidate).ping():
            form.add_error(None, "Could not verify that server URL and API key - check both and try again.")
            return render(request, _SETTINGS_PARTIAL, {"account": None, "form": form})

        from django.utils import timezone

        account, _created = ImmichAccount.objects.update_or_create(
            profile=profile,
            defaults={"server_url": candidate.server_url, "api_key": candidate.api_key, "last_verified": timezone.now()},
        )
        response = render(request, _SETTINGS_PARTIAL, {"account": account, "form": None})
        return _with_toast(response, "Immich connected.")


class ImmichDisconnectView(LoginRequiredMixin, View):
    """POST /settings/immich/disconnect/ - remove the stored Immich connection."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        ImmichAccount.objects.filter(profile=profile).delete()
        response = render(request, _SETTINGS_PARTIAL, {"account": None, "form": ImmichAccountForm()})
        return _with_toast(response, "Immich disconnected.")


# -- Pin detail: search / thumbnail / import ----------------------------------


class PinImmichSearchView(LoginRequiredMixin, View):
    """GET pin/<slug>/immich/search/ - photos on the user's Immich server, filtered by mode.

    Three modes (see ``PhotoImportMode``): nearby this pin's location, taken on
    one of the pin's recorded PinVisit dates, or unfiltered (most recent first).
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        account = ImmichAccount.objects.filter(profile=profile).first()
        mode = request.GET.get("mode", PhotoImportMode.NEARBY)
        if mode not in PhotoImportMode.values:
            mode = PhotoImportMode.NEARBY
        valid_radii = {value for value, _label in _RADIUS_CHOICES_M}
        try:
            radius_m = int(request.GET.get("radius_m", _DEFAULT_RADIUS_M))
        except (TypeError, ValueError):
            radius_m = _DEFAULT_RADIUS_M
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

        gateway = ImmichGateway(account=account)
        try:
            if mode == PhotoImportMode.VISITS:
                dates = visit_dates_for_pin(pin)
                if not dates:
                    return render(request, _PICKER_PARTIAL, {**context, "assets": [], "empty_message": "No recorded visits for this pin yet."})
                results = gateway.search_by_dates(dates)
            elif mode == PhotoImportMode.ALL:
                results = gateway.list_recent()
            else:
                if pin.location is None or pin.location.latitude is None or pin.location.longitude is None:
                    return render(request, _PICKER_PARTIAL, {**context, "error": "This pin has no location to search near."})
                pin_point = (float(pin.location.latitude), float(pin.location.longitude))
                markers = gateway.get_map_markers()
                results = [marker for marker in markers if _haversine_km(pin_point, (marker.lat, marker.lon)) * 1000 <= radius_m]
        except GatewayRequestError as exc:
            return render(request, _PICKER_PARTIAL, {**context, "error": str(exc)})

        already_imported = set(Image.objects.filter(pin=pin, profile=profile, source_url__isnull=False).values_list("source_url", flat=True))
        assets = [{"id": result.id, "already_imported": account.asset_web_url(result.id) in already_imported} for result in results]
        return render(request, _PICKER_PARTIAL, {**context, "assets": assets, "empty_message": _EMPTY_MESSAGES[mode]})


class PinImmichThumbnailView(LoginRequiredMixin, View):
    """GET pin/<slug>/immich/thumbnail/<asset_id>/ - proxies one Immich thumbnail.

    The API key must never reach the browser, so thumbnails can't be linked
    to directly - this view fetches them server-side and caches the bytes
    briefly to avoid re-hitting the user's server on every dialog reopen.
    """

    def get(self, request: HttpRequest, pin_slug: str, asset_id: str) -> HttpResponse:
        profile = _request_profile(request)
        account = get_object_or_404(ImmichAccount, profile=profile)
        cache_key = f"ul_immich_thumb_{account.pk}_{asset_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        try:
            content, content_type = ImmichGateway(account=account).get_asset_thumbnail(asset_id)
        except GatewayRequestError:
            return HttpResponse(status=502)
        cache.set(cache_key, (content, content_type), _THUMBNAIL_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)


class PinImmichImportView(LoginRequiredMixin, View):
    """POST pin/<slug>/immich/import/ - enqueue import of the selected assets."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        asset_ids = request.POST.getlist("asset_ids")
        if not asset_ids:
            return HttpResponse('<p class="immich-import-error">Select at least one photo to import.</p>', status=400)
        if not ImmichAccount.objects.filter(profile=profile).exists():
            return HttpResponse('<p class="immich-import-error">Immich is not connected.</p>', status=400)

        from urbanlens.dashboard.tasks import import_immich_photos

        result = safely_enqueue_task(import_immich_photos, pin.pk, profile.pk, asset_ids)
        if result is None:
            return render(request, _PROGRESS_PARTIAL, {"pin": pin, "state": "FAILURE", "message": "Import queue is unavailable. Please try again later."}, status=503)
        return render(request, _PROGRESS_PARTIAL, {"pin": pin, "task_id": result.id, "state": "PENDING", "percent": 0, "message": "Starting import..."})


class PinImmichImportProgressView(LoginRequiredMixin, View):
    """GET pin/<slug>/immich/import/<task_id>/progress/ - polled progress fragment."""

    def get(self, request: HttpRequest, pin_slug: str, task_id: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        progress = get_task_progress(task_id)
        context = {"pin": pin, "task_id": task_id, "state": progress.state, "percent": progress.percent, "message": progress.message, "error": progress.error}
        response = render(request, _PROGRESS_PARTIAL, context)
        if progress.state == "SUCCESS":
            result = progress.result or {}
            summary = f"Imported {result.get('imported', 0)} photo(s)" + (f", skipped {result.get('skipped')} duplicate(s)" if result.get("skipped") else "") + "."
            # refreshGallery is the same body-level event _photo_gallery.html already
            # listens for after other photo-adding actions (see visits.py).
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": summary}, "refreshGallery": {}})
        elif progress.state in {"FAILURE", "REVOKED"}:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": progress.error or "Import failed."}})
        return response
