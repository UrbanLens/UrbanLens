"""Google Photos Picker integration controller.

Two groups of views:

- Settings ("Connect Google Photos"): ``GooglePhotosSettingsView``,
  ``GooglePhotosConnectView``/``GooglePhotosCallbackView`` (OAuth2, mirrors
  Calendar's flow but a separate account/scope), ``GooglePhotosDisconnectView``.
- Pin detail ("Import from Google Photos"): create a picker session, poll it
  until the user finishes picking in Google's own UI, then the same
  thumbnail-proxy / Celery-import / progress-polling shape as the other
  providers. Every picked item is a candidate - there is no coordinate filter
  to apply here.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.photos.google import (
    GooglePhotosGateway,
    GooglePhotosNotConfiguredError,
    build_authorization_url,
    exchange_code_for_tokens,
    media_item_web_url,
    session_items_cache_key,
)
from urbanlens.dashboard.services.celery import get_task_progress, safely_enqueue_task
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.google_oauth import extract_email_from_id_token

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SETTINGS_PARTIAL = "dashboard/partials/settings/_google_photos_account.html"
_START_PARTIAL = "dashboard/partials/pins/_google_photos_picker_dialog.html"
_WAITING_PARTIAL = "dashboard/partials/pins/_google_photos_session_waiting.html"
_GRID_PARTIAL = "dashboard/partials/pins/_google_photos_picker_grid.html"
_PROGRESS_PARTIAL = "dashboard/partials/pins/_google_photos_import_progress.html"
_STATE_SALT = "google-photos-connect"
_STATE_MAX_AGE_SECONDS = 600
_SESSION_ITEMS_CACHE_TTL = 3600


def _request_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _with_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


def _session_owner_cache_key(session_id: str) -> str:
    return f"ul_gphotos_session_owner_{session_id}"


def _require_session_owner(session_id: str, profile: Profile) -> None:
    """Raise Http404 unless ``profile`` created this picker session.

    Args:
        session_id: The picker session id from the URL.
        profile: The requesting profile.

    Raises:
        Http404: When the session is unknown or belongs to someone else.
    """
    owner_pid = cache.get(_session_owner_cache_key(session_id))
    if owner_pid != profile.id:
        raise Http404("Unknown picker session.")


# -- Settings: connect / disconnect -------------------------------------------


class GooglePhotosSettingsView(LoginRequiredMixin, View):
    """GET /settings/google-photos/ - HTMX subsection showing the current connection state."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        account = GooglePhotosAccount.objects.filter(profile=profile).first()
        return render(request, _SETTINGS_PARTIAL, {"account": account})


class GooglePhotosConnectView(LoginRequiredMixin, View):
    """GET /settings/google-photos/connect/ - start the OAuth consent flow."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        state = signing.dumps({"pid": profile.id}, salt=_STATE_SALT)
        try:
            url = build_authorization_url(request.build_absolute_uri(reverse("settings.google_photos.callback")), state)
        except GooglePhotosNotConfiguredError:
            messages.error(request, "Google Photos integration is not configured on this server.")
            return redirect("settings.view")
        return redirect(url)


class GooglePhotosCallbackView(LoginRequiredMixin, View):
    """GET /settings/google-photos/callback/ - exchange the code and store the user's tokens."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        if request.GET.get("error"):
            messages.error(request, "Google Photos access was not granted.")
            return redirect("settings.view")

        state = request.GET.get("state") or ""
        code = request.GET.get("code") or ""
        try:
            payload = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE_SECONDS)
        except signing.BadSignature:
            messages.error(request, "The Google Photos connection request was invalid or expired. Please try again.")
            return redirect("settings.view")
        if payload.get("pid") != profile.id or not code:
            messages.error(request, "The Google Photos connection request was invalid or expired. Please try again.")
            return redirect("settings.view")

        try:
            tokens = exchange_code_for_tokens(code, request.build_absolute_uri(reverse("settings.google_photos.callback")))
        except (GooglePhotosNotConfiguredError, GatewayRequestError):
            messages.error(request, "Google Photos authorization failed.")
            return redirect("settings.view")

        expires_in = int(tokens.get("expires_in") or 3600)
        GooglePhotosAccount.objects.update_or_create(
            profile=profile,
            defaults={
                "google_email": extract_email_from_id_token(tokens.get("id_token")),
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token") or "",
                "token_expiry": timezone.now() + datetime.timedelta(seconds=expires_in),
            },
        )
        messages.success(request, "Google Photos connected.")
        return redirect("settings.view")


class GooglePhotosDisconnectView(LoginRequiredMixin, View):
    """POST /settings/google-photos/disconnect/ - remove the stored Google Photos connection."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        GooglePhotosAccount.objects.filter(profile=profile).delete()
        response = render(request, _SETTINGS_PARTIAL, {"account": None})
        return _with_toast(response, "Google Photos disconnected.")


# -- Pin detail: session / picker / import -------------------------------------


class PinGooglePhotosStartView(LoginRequiredMixin, View):
    """GET pin/<slug>/google-photos/ - initial tab state (connect prompt or start-session button)."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        account = GooglePhotosAccount.objects.filter(profile=profile).first()
        context = {"pin": pin, "account": account}
        if account is not None and not profile.external_apis_enabled:
            context["error"] = "External lookups are turned off in your settings."
        return render(request, _START_PARTIAL, context)


class PinGooglePhotosSessionCreateView(LoginRequiredMixin, View):
    """POST pin/<slug>/google-photos/session/ - create a picker session."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        account = get_object_or_404(GooglePhotosAccount, profile=profile)

        try:
            picker_session = GooglePhotosGateway(account=account).create_session()
        except GatewayRequestError as exc:
            return render(request, _START_PARTIAL, {"pin": pin, "account": account, "error": str(exc)})

        cache.set(_session_owner_cache_key(picker_session.id), profile.id, picker_session.timeout_s + 60)
        return render(
            request,
            _WAITING_PARTIAL,
            {"pin": pin, "session_id": picker_session.id, "picker_uri": picker_session.picker_uri, "poll_interval_s": picker_session.poll_interval_s},
        )


class PinGooglePhotosSessionStatusView(LoginRequiredMixin, View):
    """GET pin/<slug>/google-photos/session/<session_id>/status/ - polled session status."""

    def get(self, request: HttpRequest, pin_slug: str, session_id: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        _require_session_owner(session_id, profile)
        account = get_object_or_404(GooglePhotosAccount, profile=profile)
        gateway = GooglePhotosGateway(account=account)

        try:
            picker_session = gateway.get_session(session_id)
        except GatewayRequestError as exc:
            return render(request, _START_PARTIAL, {"pin": pin, "account": account, "error": str(exc)})

        if not picker_session.media_items_set:
            return render(
                request,
                _WAITING_PARTIAL,
                {"pin": pin, "session_id": session_id, "picker_uri": picker_session.picker_uri, "poll_interval_s": picker_session.poll_interval_s},
            )

        try:
            items = gateway.list_session_media_items(session_id)
        except GatewayRequestError as exc:
            return render(request, _START_PARTIAL, {"pin": pin, "account": account, "error": str(exc)})

        cache.set(
            session_items_cache_key(session_id),
            {item.id: {"base_url": item.base_url, "mime_type": item.mime_type, "filename": item.filename} for item in items},
            _SESSION_ITEMS_CACHE_TTL,
        )
        already_imported = set(Image.objects.filter(pin=pin, profile=profile, source_url__isnull=False).values_list("source_url", flat=True))
        assets = [{"id": item.id, "already_imported": media_item_web_url(item.id) in already_imported} for item in items]
        return render(request, _GRID_PARTIAL, {"pin": pin, "session_id": session_id, "assets": assets})


class PinGooglePhotosThumbnailView(LoginRequiredMixin, View):
    """GET pin/<slug>/google-photos/thumbnail/<session_id>/<item_id>/ - proxies one picked item's preview.

    The Bearer token must never reach the browser, and Google's ``baseUrl``
    requires it - this view fetches the preview server-side, same reasoning
    as the Immich thumbnail proxy.
    """

    def get(self, request: HttpRequest, pin_slug: str, session_id: str, item_id: str) -> HttpResponse:
        profile = _request_profile(request)
        _require_session_owner(session_id, profile)
        cache_key = f"ul_gphotos_thumb_{session_id}_{item_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        items = cache.get(session_items_cache_key(session_id)) or {}
        item = items.get(item_id)
        if item is None:
            return HttpResponse(status=404)

        account = get_object_or_404(GooglePhotosAccount, profile=profile)
        try:
            content = GooglePhotosGateway(account=account).download_media_item(item["base_url"], original=False)
        except GatewayRequestError:
            return HttpResponse(status=502)
        content_type = item.get("mime_type", "image/jpeg")
        cache.set(cache_key, (content, content_type), _SESSION_ITEMS_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)


class PinGooglePhotosImportView(LoginRequiredMixin, View):
    """POST pin/<slug>/google-photos/import/ - enqueue import of the selected items."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
        profile = _request_profile(request)
        session_id = request.POST.get("session_id") or ""
        media_item_ids = request.POST.getlist("media_item_ids")
        if session_id:
            _require_session_owner(session_id, profile)
        if not media_item_ids:
            return HttpResponse('<p class="immich-import-error">Select at least one photo to import.</p>', status=400)
        if not GooglePhotosAccount.objects.filter(profile=profile).exists():
            return HttpResponse('<p class="immich-import-error">Google Photos is not connected.</p>', status=400)

        from urbanlens.dashboard.tasks import import_google_photos

        result = safely_enqueue_task(import_google_photos, pin.pk, profile.pk, session_id, media_item_ids)
        if result is None:
            return render(request, _PROGRESS_PARTIAL, {"pin": pin, "state": "FAILURE", "message": "Import queue is unavailable. Please try again later."}, status=503)
        return render(request, _PROGRESS_PARTIAL, {"pin": pin, "task_id": result.id, "state": "PENDING", "percent": 0, "message": "Starting import..."})


class PinGooglePhotosImportProgressView(LoginRequiredMixin, View):
    """GET pin/<slug>/google-photos/import/<task_id>/progress/ - polled progress fragment."""

    def get(self, request: HttpRequest, pin_slug: str, task_id: str) -> HttpResponse:
        pin = get_object_or_404(Pin, slug=pin_slug)
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
