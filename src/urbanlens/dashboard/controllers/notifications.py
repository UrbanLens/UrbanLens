"""Notification bell dropdown and preferences controllers."""
from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference

logger = logging.getLogger(__name__)

_PREF_FIELDS = [
    ("trip_updated", "Trip Updated"),
    ("friend_request", "Friend Request Received"),
    ("message", "Message Received"),
    ("comment_reply", "Reply to Comment"),
    ("comment_liked", "Comment Liked"),
    ("friend_accepted", "Friend Request Accepted"),
    ("added_to_trip", "Added to Trip"),
    ("wiki_updated", "Community Wiki Updated"),
]


def _get_or_create_prefs(profile) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(profile=profile)
    return prefs


class NotificationDropdownView(LoginRequiredMixin, View):
    """GET /notifications/dropdown/ — renders the bell dropdown partial."""

    def get(self, request):
        profile = request.user.profile
        notifications = (
            NotificationLog.objects
            .for_profile(profile)
            .order_by("-created")[:20]
        )
        unread_count = NotificationLog.objects.for_profile(profile).unread().count()
        return render(request, "dashboard/partials/notification_dropdown.html", {
            "notifications": notifications,
            "unread_count": unread_count,
        })


class NotificationMarkReadView(LoginRequiredMixin, View):
    """POST /notifications/<id>/read/ — mark one notification as read."""

    def post(self, request, notification_id):
        profile = request.user.profile
        notification = get_object_or_404(NotificationLog, id=notification_id, profile=profile)
        notification.status = Status.READ
        notification.save(update_fields=["status", "updated"])
        return HttpResponse("", status=204)


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    """POST /notifications/read-all/ — mark all notifications as read."""

    def post(self, request):
        profile = request.user.profile
        NotificationLog.objects.for_profile(profile).unread().mark_read()
        return render(request, "dashboard/partials/notification_dropdown.html", {
            "notifications": NotificationLog.objects.for_profile(profile).order_by("-created")[:20],
            "unread_count": 0,
        })


class NotificationPreferencesView(LoginRequiredMixin, View):
    """GET/POST /notifications/preferences/ — view or save per-type delivery prefs."""

    def get(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
        return render(request, "dashboard/partials/notification_preferences.html", {
            "prefs": prefs,
            "pref_fields": _PREF_FIELDS,
            "delivery_choices": DeliveryPreference.choices,
        })

    def post(self, request):
        profile = request.user.profile
        prefs = _get_or_create_prefs(profile)
        valid = {v for v, _ in DeliveryPreference.choices}
        for field, _ in _PREF_FIELDS:
            val = request.POST.get(field, "")
            if val in valid:
                setattr(prefs, field, val)
        prefs.save()
        return render(request, "dashboard/partials/notification_preferences.html", {
            "prefs": prefs,
            "pref_fields": _PREF_FIELDS,
            "delivery_choices": DeliveryPreference.choices,
            "saved": True,
        })


class NotificationUnreadCountView(LoginRequiredMixin, View):
    """GET /notifications/unread-count/ — returns the unread count badge partial."""

    def get(self, request):
        profile = request.user.profile
        count = NotificationLog.objects.for_profile(profile).unread().count()
        return render(request, "dashboard/partials/notification_badge.html", {"unread_count": count})
