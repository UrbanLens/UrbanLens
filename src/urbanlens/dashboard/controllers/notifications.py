"""Notification bell dropdown, history page, and preferences controllers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.notifications.notification_center import (
    get_preferences,
    inbox_notifications,
    mark_all_read,
    unread_count,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_PREF_FIELDS = [
    ("friend_request", "New Friend Request"),
    ("friend_accepted", "Friend Request Accepted"),
    ("message", "New Message"),
    ("comment_reply", "Reply to Comment"),
    ("comment_liked", "Comment Likes"),
    ("pin_shared", "Pin Shared"),
    ("visit_suggested", "Visit Suggested"),
    ("added_to_trip", "Trip Invitation"),
    ("trip_updated", "Trip Updated"),
    ("wiki_updated", "Community Wiki Updated"),
    ("wiki_safety_checkin", "Safety Check-in at a Pinned Location"),
    ("safety_checkin_partner_invite", "Safety Check-in Partner Invitation"),
    ("achievement_earned", "Achievement Unlocked"),
]

_HISTORY_PAGE_SIZE = 30


def _get_or_create_prefs(profile: Profile) -> NotificationPreference:
    """Return the profile's preference row, creating the default one if absent.

    Thin alias kept for this module's existing callers; the implementation
    lives in ``services.notifications.notification_center`` so the external API shares it.

    Args:
        profile: The owner whose preferences to read.

    Returns:
        The profile's ``NotificationPreference``.
    """
    return get_preferences(profile)


def _trigger_label_refresh(response: HttpResponse) -> HttpResponse:
    """Attach an HTMX trigger so the nav bell label refreshes."""
    response["HX-Trigger"] = json.dumps({"notifCountRefresh": {"target": "body"}})
    return response


def _merge_triggers(response: HttpResponse, triggers: dict[str, Any]) -> HttpResponse:
    """Merge ``triggers`` into any existing ``HX-Trigger`` header on ``response``."""
    existing_raw = response.get("HX-Trigger")
    merged: dict[str, Any] = {}
    if existing_raw:
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, dict):
                merged.update(parsed)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str):
                        merged[item] = True
        except (TypeError, ValueError, json.JSONDecodeError):
            merged[existing_raw] = True
    merged.update(triggers)
    response["HX-Trigger"] = json.dumps(merged)
    return response


def action_taken_response(
    request: HttpRequest,
    profile: Profile,
    *,
    notification: NotificationLog | None = None,
    extra_triggers: dict[str, Any] | None = None,
) -> HttpResponse:
    """HTMX response after the user answers an actionable notification.

    From the bell dropdown (``surface=inbox``, the default), returns an empty
    body so HTMX can animate the row out. From the history page
    (``surface=history``), re-renders the settled notification row in place.

    Args:
        request: Incoming request (reads ``surface`` from POST).
        profile: Acting profile.
        notification: The notification that was answered, when known (required
            for history-page re-render).
        extra_triggers: Additional ``HX-Trigger`` events to merge in.

    Returns:
        An HTMX-friendly response with a label-refresh trigger.
    """
    surface = request.POST.get("surface", "inbox")
    triggers: dict[str, Any] = {"notifCountRefresh": {"target": "body"}}
    if extra_triggers:
        triggers.update(extra_triggers)

    if surface == "history" and notification is not None:
        n = get_object_or_404(
            NotificationLog.objects.for_display(),
            pk=notification.pk,
            profile=profile,
        )
        response = render(
            request,
            "dashboard/partials/notifications/notification_item.html",
            {"n": n, "notif_surface": "history"},
        )
        return _merge_triggers(response, triggers)

    response = HttpResponse("")
    return _merge_triggers(response, triggers)


def _render_dropdown(request: HttpRequest, profile: Profile) -> HttpResponse:
    """Render the bell dropdown partial for ``profile``."""
    notifications = inbox_notifications(profile)
    unread_ids = [n.id for n in notifications if n.is_unread]
    if unread_ids:
        NotificationLog.objects.filter(id__in=unread_ids).mark_read()
        for n in notifications:
            if n.id in unread_ids:
                n.status = Status.READ
    response = render(
        request,
        "dashboard/partials/notifications/notification_dropdown.html",
        {
            "notifications": notifications,
            "unread_count": unread_count(profile),
            "notif_surface": "inbox",
        },
    )
    return _trigger_label_refresh(response) if unread_ids else response


class NotificationDropdownView(LoginRequiredMixin, View):
    """GET /notifications/dropdown/ - renders the bell dropdown partial.

    Viewing the dropdown marks its notifications read (UL-348) - not just clicking
    one individually. Action buttons (accept/decline friend request, pin share,
    visit suggestion) are gated on the underlying request's own pending state, not
    on notification read/unread, so this doesn't hide anything still actionable.
    Dismissed (already-answered) rows are excluded; see the history page for those.
    """

    def get(self, request):
        return _render_dropdown(request, request.user.profile)


class NotificationHistoryView(LoginRequiredMixin, View):
    """GET /notifications/ - full notification history (current + acted-on)."""

    def get(self, request):
        profile = request.user.profile
        qs = NotificationLog.objects.for_profile(profile).for_display().order_by("-created")
        page_obj = get_page(request, qs, _HISTORY_PAGE_SIZE)
        context = {
            "page_name": "notifications",
            "notifications": page_obj.object_list,
            "page_obj": page_obj,
            "notif_surface": "history",
            "unread_count": unread_count(profile),
        }
        if request.headers.get("HX-Request"):
            return render(request, "dashboard/partials/notifications/notification_history_card.html", context)
        return render(request, "dashboard/pages/notifications/index.html", context)


class NotificationMarkReadView(LoginRequiredMixin, View):
    """POST /notifications/<id>/read/ - mark one notification as read."""

    def post(self, request, notification_id):
        profile = request.user.profile
        notification = get_object_or_404(
            NotificationLog.objects.for_display(),
            id=notification_id,
            profile=profile,
        )
        if notification.status == Status.UNREAD:
            notification.status = Status.READ
            notification.save(update_fields=["status", "updated"])
        response = render(
            request,
            "dashboard/partials/notifications/notification_item.html",
            {"n": notification, "notif_surface": request.POST.get("surface", "inbox")},
        )
        return _trigger_label_refresh(response)


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    """POST /notifications/read-all/ - mark all notifications as read."""

    def post(self, request):
        profile = request.user.profile
        mark_all_read(profile)
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {
                "notifications": inbox_notifications(profile),
                "unread_count": 0,
                "notif_surface": "inbox",
            },
        )
        return _trigger_label_refresh(response)


class NotificationPreferencesView(LoginRequiredMixin, View):
    """GET/POST /notifications/preferences/ - view or save per-type delivery prefs."""

    def _render(self, request, profile: Profile, prefs, *, saved: bool = False) -> HttpResponse:
        return render(
            request,
            "dashboard/partials/notifications/notification_preferences.html",
            {
                "prefs": prefs,
                "pref_fields": _PREF_FIELDS,
                "saved": saved,
                # WhatsApp/SMS delivery only makes sense once the profile has a
                # number to deliver to - the template disables those columns
                # (without touching stored preferences) until then.
                "has_whatsapp_number": bool(profile.whatsapp_number),
                "has_phone_number": bool(profile.phone_number),
            },
        )

    def get(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
        return self._render(request, profile, prefs)

    def post(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
        # Mirrors the template's disabled WhatsApp/SMS columns: without a
        # number on file there's nowhere to deliver to, so neither channel
        # can be turned on server-side either, regardless of what a client sends.
        can_whatsapp = bool(profile.whatsapp_number)
        can_sms = bool(profile.phone_number)
        for field, _ in _PREF_FIELDS:
            site = f"{field}__site" in request.POST
            email = f"{field}__email" in request.POST
            if site and email:
                value = DeliveryPreference.BOTH
            elif site:
                value = DeliveryPreference.SITE
            elif email:
                value = DeliveryPreference.EMAIL
            else:
                value = DeliveryPreference.NONE
            setattr(prefs, field, value)
            setattr(prefs, f"{field}_whatsapp", can_whatsapp and f"{field}_whatsapp" in request.POST)
            setattr(prefs, f"{field}_sms", can_sms and f"{field}_sms" in request.POST)
        prefs.save()
        return self._render(request, profile, prefs, saved=True)


class NotificationUnreadCountView(LoginRequiredMixin, View):
    """GET /notifications/unread-count/ - returns the unread count label partial."""

    def get(self, request):
        return render(
            request,
            "dashboard/partials/notifications/notification_label.html",
            {"unread_count": unread_count(request.user.profile)},
        )
