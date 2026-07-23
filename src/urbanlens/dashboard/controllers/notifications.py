"""Notification bell dropdown and preferences controllers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference

if TYPE_CHECKING:
    from django.http import HttpResponse

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
]


def _get_or_create_prefs(profile: Profile) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(profile=profile)
    return prefs


def _trigger_label_refresh(response: HttpResponse) -> HttpResponse:
    """Attach an HTMX trigger so the nav bell label refreshes."""
    response["HX-Trigger"] = json.dumps({"notifCountRefresh": {"target": "body"}})
    return response


class NotificationDropdownView(LoginRequiredMixin, View):
    """GET /notifications/dropdown/ - renders the bell dropdown partial.

    Viewing the dropdown marks its notifications read (UL-348) - not just clicking
    one individually. Action buttons (accept/decline friend request, pin share,
    visit suggestion) are gated on the underlying request's own pending state, not
    on notification read/unread, so this doesn't hide anything still actionable.
    """

    def get(self, request):
        profile = request.user.profile
        notifications = list(NotificationLog.objects.for_profile(profile).select_related("source_profile").order_by("-created")[:20])
        unread_ids = [n.id for n in notifications if n.is_unread]
        if unread_ids:
            NotificationLog.objects.filter(id__in=unread_ids).mark_read()
            for n in notifications:
                if n.id in unread_ids:
                    n.status = Status.READ
        unread_count = NotificationLog.objects.for_profile(profile).unread().count()
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
            },
        )
        return _trigger_label_refresh(response) if unread_ids else response


class NotificationMarkReadView(LoginRequiredMixin, View):
    """POST /notifications/<id>/read/ - mark one notification as read."""

    def post(self, request, notification_id):
        profile = request.user.profile
        notification = get_object_or_404(
            NotificationLog.objects.select_related("source_profile"),
            id=notification_id,
            profile=profile,
        )
        if notification.status != Status.READ:
            notification.status = Status.READ
            notification.save(update_fields=["status", "updated"])
        response = render(
            request,
            "dashboard/partials/notifications/notification_item.html",
            {"n": notification},
        )
        return _trigger_label_refresh(response)


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    """POST /notifications/read-all/ - mark all notifications as read."""

    def post(self, request):
        profile = request.user.profile
        NotificationLog.objects.for_profile(profile).unread().mark_read()
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {
                "notifications": NotificationLog.objects.for_profile(profile).select_related("source_profile").order_by("-created")[:20],
                "unread_count": 0,
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
        profile = request.user.profile
        count = NotificationLog.objects.for_profile(profile).unread().count()
        return render(request, "dashboard/partials/notifications/notification_label.html", {"unread_count": count})
