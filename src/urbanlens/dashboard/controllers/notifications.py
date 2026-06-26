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
    ("trip_updated", "Trip Updated"),
    ("friend_request", "New Friend Request"),
    ("message", "New Message"),
    ("comment_reply", "Reply to Comment"),
    ("comment_liked", "Comment Likes"),
    ("friend_accepted", "Friend Request Accepted"),
    ("added_to_trip", "Trip Invitation"),
    ("wiki_updated", "Community Wiki Updated"),
]


def _get_or_create_prefs(profile: Profile) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(profile=profile)
    return prefs


def _trigger_badge_refresh(response: HttpResponse) -> HttpResponse:
    """Attach an HTMX trigger so the nav bell badge refreshes."""
    response["HX-Trigger"] = json.dumps({"notifCountRefresh": {}})
    return response


class NotificationDropdownView(LoginRequiredMixin, View):
    """GET /notifications/dropdown/ - renders the bell dropdown partial."""

    def get(self, request):
        profile = request.user.profile
        notifications = (
            NotificationLog.objects.for_profile(profile).select_related("source_profile").order_by("-created")[:20]
        )
        unread_count = NotificationLog.objects.for_profile(profile).unread().count()
        return render(
            request,
            "dashboard/partials/notification_dropdown.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
            },
        )


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
            "dashboard/partials/notification_item.html",
            {"n": notification},
        )
        return _trigger_badge_refresh(response)


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    """POST /notifications/read-all/ - mark all notifications as read."""

    def post(self, request):
        profile = request.user.profile
        NotificationLog.objects.for_profile(profile).unread().mark_read()
        response = render(
            request,
            "dashboard/partials/notification_dropdown.html",
            {
                "notifications": NotificationLog.objects.for_profile(profile)
                .select_related("source_profile")
                .order_by("-created")[:20],
                "unread_count": 0,
            },
        )
        return _trigger_badge_refresh(response)


class NotificationPreferencesView(LoginRequiredMixin, View):
    """GET/POST /notifications/preferences/ - view or save per-type delivery prefs."""

    def _render(self, request, prefs, *, saved: bool = False) -> HttpResponse:
        return render(
            request,
            "dashboard/partials/notification_preferences.html",
            {
                "prefs": prefs,
                "pref_fields": _PREF_FIELDS,
                "saved": saved,
            },
        )

    def get(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
        return self._render(request, prefs)

    def post(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
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
        prefs.save()
        return self._render(request, prefs, saved=True)


class NotificationUnreadCountView(LoginRequiredMixin, View):
    """GET /notifications/unread-count/ - returns the unread count badge partial."""

    def get(self, request):
        profile = request.user.profile
        count = NotificationLog.objects.for_profile(profile).unread().count()
        return render(request, "dashboard/partials/notification_badge.html", {"unread_count": count})
