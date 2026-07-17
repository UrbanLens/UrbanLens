"""Google Calendar integration controllers.

Connect/disconnect a user's own Google Calendar via OAuth, import calendar
events as trips, and export trips as calendar events. Every view operates on
the *requesting user's* connected account - there is no shared site
calendar.
"""

from __future__ import annotations

import datetime
import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.controllers.trip import _trip_or_403, _trips_for_list
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.calendar.google import (
    CalendarNotConfiguredError,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_email_from_id_token,
    revoke_token,
)
from urbanlens.dashboard.services.calendar_sync import (
    build_import_preview,
    export_trip_to_calendar,
    import_events_as_trips,
    list_importable_events,
    remove_trip_from_calendar,
)
from urbanlens.dashboard.services.gateway import GatewayRequestError

logger = logging.getLogger(__name__)

_STATE_SALT = "google-calendar-connect"
_STATE_MAX_AGE_SECONDS = 600
#: Named views the connect flow may return to via ``?next=`` - never redirect
#: to an arbitrary value from user input, only one of these known routes.
_ALLOWED_NEXT_VIEW_NAMES = {"trips.list", "settings.view"}


def _resolve_next_view_name(name: str | None) -> str:
    """Return `name` if it's an allowed post-connect redirect target, else the default."""
    return name if name in _ALLOWED_NEXT_VIEW_NAMES else "trips.list"


def _callback_uri(request: HttpRequest) -> str:
    """Absolute OAuth callback URL for this deployment.

    Args:
        request: The current request (used for scheme/host).

    Returns:
        The absolute URL of the calendar OAuth callback view.
    """
    return request.build_absolute_uri(reverse("trips.calendar.callback"))


def calendar_context(profile: Profile, trip=None) -> dict:
    """Template context describing the viewer's calendar connection state.

    Args:
        profile: The viewing profile.
        trip: Optional trip, to include that trip's export link for this user.

    Returns:
        Dict with ``calendar_account`` and (when a trip is given)
        ``calendar_link`` keys.
    """
    account = GoogleCalendarAccount.objects.get_for_profile(profile)
    context: dict = {"calendar_account": account}
    if trip is not None:
        context["calendar_link"] = TripCalendarLink.objects.trip_level_link(trip, profile) if account else None
    return context


class GoogleCalendarConnectView(LoginRequiredMixin, View):
    """Start the OAuth consent flow for the user's own Google Calendar.

    GET /trips/calendar/connect/?next=<view name>
    """

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        next_name = _resolve_next_view_name(request.GET.get("next"))
        state = signing.dumps({"pid": profile.id, "next": next_name}, salt=_STATE_SALT)
        try:
            url = build_authorization_url(_callback_uri(request), state)
        except CalendarNotConfiguredError:
            messages.error(request, "Google Calendar integration is not configured on this server.")
            return redirect(next_name)
        return redirect(url)


class GoogleCalendarCallbackView(LoginRequiredMixin, View):
    """OAuth callback: exchange the code and store the user's tokens.

    GET /trips/calendar/callback/
    """

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        state = request.GET.get("state") or ""
        try:
            payload = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE_SECONDS)
        except signing.BadSignature:
            payload = {}
        next_name = _resolve_next_view_name(payload.get("next"))

        if request.GET.get("error"):
            messages.error(request, "Google Calendar access was not granted.")
            return redirect(next_name)

        code = request.GET.get("code") or ""
        if payload.get("pid") != profile.id or not code:
            messages.error(request, "The calendar connection request was invalid or expired. Please try again.")
            return redirect(next_name)

        try:
            tokens = exchange_code_for_tokens(code, _callback_uri(request))
        except (CalendarNotConfiguredError, GatewayRequestError):
            logger.exception("Google Calendar token exchange failed for profile %s", profile.id)
            messages.error(request, "Connecting to Google Calendar failed. Please try again.")
            return redirect(next_name)

        expires_in = int(tokens.get("expires_in") or 3600)
        account, _created = GoogleCalendarAccount.objects.update_or_create(
            profile=profile,
            defaults={
                "access_token": tokens["access_token"],
                "token_expiry": timezone.now() + datetime.timedelta(seconds=expires_in),
                "google_email": extract_email_from_id_token(tokens.get("id_token")),
                "scopes": tokens.get("scope") or "",
            },
        )
        # A refresh token is only issued on fresh consent; keep the old one
        # when Google omits it on a re-connect.
        if tokens.get("refresh_token"):
            account.refresh_token = tokens["refresh_token"]
            account.save(update_fields=["refresh_token", "updated"])

        messages.success(request, "Google Calendar connected. You can now import events and export trips.")
        return redirect(next_name)


class GoogleCalendarDisconnectView(LoginRequiredMixin, View):
    """Revoke and remove the user's calendar connection.

    POST /trips/calendar/disconnect/
    """

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is not None:
            revoke_token(account.refresh_token or account.access_token)
            account.delete()
        messages.info(request, "Google Calendar disconnected.")
        response = HttpResponse("", status=200)
        response["HX-Redirect"] = reverse("trips.list")
        return response


_SETTINGS_PARTIAL = "dashboard/partials/settings/_google_calendar_account.html"


class GoogleCalendarSettingsSectionView(LoginRequiredMixin, View):
    """GET /settings/google-calendar/ - HTMX subsection showing the current connection state."""

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(request, _SETTINGS_PARTIAL, {"calendar_account": GoogleCalendarAccount.objects.get_for_profile(profile)})


class GoogleCalendarSettingsDisconnectView(LoginRequiredMixin, View):
    """POST /settings/google-calendar/disconnect/ - disconnect and re-render the settings subsection.

    Separate from ``GoogleCalendarDisconnectView`` because that view always
    issues an ``HX-Redirect`` to the trips list (matching where its only other
    caller - the calendar import dialog - lives); redirecting away would be a
    jarring way to leave the Settings page after clicking "Disconnect" here.
    """

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is not None:
            revoke_token(account.refresh_token or account.access_token)
            account.delete()
        response = render(request, _SETTINGS_PARTIAL, {"calendar_account": None})
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "info", "message": "Google Calendar disconnected."}})
        return response


class CalendarImportView(LoginRequiredMixin, View):
    """Import dialog and import action for the user's calendar events.

    GET  /trips/calendar/import/  → dialog listing upcoming events
    POST /trips/calendar/import/  → create trips from selected events
    """

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is None:
            return render(request, "dashboard/partials/trips/_calendar_import_dialog.html", {"error": "Connect your Google Calendar first.", "profile": profile})

        error = ""
        entries: list[dict] = []
        try:
            entries = list_importable_events(account)
        except GatewayRequestError as exc:
            error = str(exc)

        return render(
            request,
            "dashboard/partials/trips/_calendar_import_dialog.html",
            {
                "account": account,
                "entries": entries,
                "error": error,
                "profile": profile,
            },
        )

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is None:
            # Plain-text 4xx bodies surface via the global htmx:responseError toast.
            return HttpResponse("Connect your Google Calendar first.", status=400)

        event_ids = [eid for eid in request.POST.getlist("event_ids") if eid.strip()]
        if not event_ids:
            return HttpResponse("Select at least one event to import.", status=400)

        selections = [
            {
                "event_id": event_id,
                "create_activity": request.POST.get(f"create_activity_{event_id}") == "1",
                "invite_profile_ids": [int(value) for value in request.POST.getlist(f"invite_{event_id}") if value.isdigit()],
                "auto_sync": request.POST.get(f"auto_sync_{event_id}") == "1",
            }
            for event_id in event_ids
        ]

        try:
            created, skipped, invited = import_events_as_trips(account, selections)
        except GatewayRequestError as exc:
            return HttpResponse(str(exc), status=502)

        trips = _trips_for_list(profile)
        response = render(request, "dashboard/partials/trips/trip_list_partial.html", {"trips": trips, "profile": profile})
        if created:
            message = f"Imported {len(created)} event{'s' if len(created) != 1 else ''} as trips."
            level = "success"
            if invited:
                message += f" Invited {invited} participant{'s' if invited != 1 else ''}."
        else:
            message = "No events were imported."
            level = "warning"
        if skipped:
            message += f" {skipped[0]}" if len(skipped) == 1 else f" {len(skipped)} items were skipped."
        response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
        return response


class CalendarImportPreviewView(LoginRequiredMixin, View):
    """Second page of the import dialog: review trips, activities, and invitations.

    POST /trips/calendar/import/preview/  → render the review step for the
    events selected on page one. Nothing is created here - the user can still
    uncheck activities and participants before confirming.
    """

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is None:
            return HttpResponse("Connect your Google Calendar first.", status=400)

        event_ids = [eid for eid in request.POST.getlist("event_ids") if eid.strip()]
        if not event_ids:
            return HttpResponse("Select at least one event to import.", status=400)

        try:
            previews = build_import_preview(account, event_ids)
        except GatewayRequestError as exc:
            return HttpResponse(str(exc), status=502)

        importable = [entry for entry in previews if not entry["skip_reason"]]
        return render(
            request,
            "dashboard/partials/trips/_calendar_import_preview.html",
            {
                "account": account,
                "previews": previews,
                "importable_count": len(importable),
                "profile": profile,
            },
        )


class TripCalendarExportView(LoginRequiredMixin, View):
    """Export a trip to (or remove it from) the user's own Google Calendar.

    POST   /trips/<slug>/calendar/export/  → create/update the event
    DELETE /trips/<slug>/calendar/export/  → delete the event
    Both re-render the trip's calendar-button partial.
    """

    def _render_button(self, request: HttpRequest, trip, profile: Profile, *, toast: tuple[str, str] | None = None, status: int = 200) -> HttpResponse:
        """Re-render the calendar button partial, optionally with a toast.

        Args:
            request: The HTTP request.
            trip: The trip being displayed.
            profile: The viewing profile.
            toast: Optional (level, message) toast to trigger.
            status: HTTP status code.

        Returns:
            Rendered partial response.
        """
        context = {"trip": trip, "profile": profile, **calendar_context(profile, trip)}
        response = render(request, "dashboard/partials/trips/_trip_calendar_button.html", context, status=status)
        if toast:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": toast[0], "message": toast[1]}})
        return response

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is None:
            return self._render_button(request, trip, profile, toast=("warning", "Connect your Google Calendar first."), status=200)

        trip_url = request.build_absolute_uri(reverse("trips.detail", kwargs={"trip_slug": trip.slug}))
        try:
            link, activity_count = export_trip_to_calendar(account, trip, trip_url=trip_url)
        except ValueError as exc:
            return self._render_button(request, trip, profile, toast=("warning", str(exc)))
        except GatewayRequestError as exc:
            return self._render_button(request, trip, profile, toast=("error", str(exc)))

        auto_sync = request.POST.get("auto_sync") == "1"
        if link.auto_sync != auto_sync:
            TripCalendarLink.objects.set_auto_sync(link.pk, auto_sync)

        if activity_count:
            message = f"Trip and {activity_count} activit{'ies' if activity_count != 1 else 'y'} added to your Google Calendar."
        else:
            message = "Trip added to your Google Calendar."
        return self._render_button(request, trip, profile, toast=("success", message))

    def delete(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        account = GoogleCalendarAccount.objects.get_for_profile(profile)
        if account is None:
            return self._render_button(request, trip, profile, toast=("warning", "Connect your Google Calendar first."))

        try:
            removed = remove_trip_from_calendar(account, trip)
        except GatewayRequestError as exc:
            return self._render_button(request, trip, profile, toast=("error", str(exc)))

        toast = ("success", "Trip removed from your Google Calendar.") if removed else ("info", "This trip was not on your Google Calendar.")
        return self._render_button(request, trip, profile, toast=toast)


class TripCalendarAutoSyncView(LoginRequiredMixin, View):
    """Toggle whether an already-exported trip keeps pushing future edits to its calendar event.

    POST /trips/<slug>/calendar/auto-sync/  → flip TripCalendarLink.auto_sync for the
    viewing profile's export link. Does not touch the calendar itself - it only
    changes whether *later* saves trigger a push. Re-renders the calendar button.
    """

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        link = TripCalendarLink.objects.trip_level_link(trip, profile)
        if link is None:
            return HttpResponse("Add this trip to your Google Calendar first.", status=400)

        auto_sync = request.POST.get("auto_sync") == "1"
        TripCalendarLink.objects.set_auto_sync(link.pk, auto_sync)

        context = {"trip": trip, "profile": profile, **calendar_context(profile, trip)}
        return render(request, "dashboard/partials/trips/_trip_calendar_button.html", context)
