"""Controllers for sharing a single pin with one friend."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.connections import are_connections, get_connections


def _recipient_has_pin(profile: Profile, source: Pin) -> bool:
    # A Pin's coordinates live on its Location, so "same place" == same Location.
    if not source.location_id:
        return False
    return Pin.objects.filter(
        profile=profile,
        parent_pin__isnull=True,
        parent_wiki__isnull=True,
        location_id=source.location_id,
    ).exists()


def _create_pin_from_share(share: PinShare) -> Pin:
    source = share.pin
    new_pin = Pin.objects.create(
        profile=share.to_profile,
        location=source.location,
        is_private=source.is_private,
        name=source.name,
        name_is_user_provided=source.name_is_user_provided,
        icon=source.icon,
        description=source.description,
        priority=source.priority,
        vulnerability=source.vulnerability,
        danger=source.danger,
        pin_type=source.pin_type,
        color=source.color,
        date_abandoned=source.date_abandoned,
        date_last_active=source.date_last_active,
        fences=source.fences,
        alarms=source.alarms,
        cameras=source.cameras,
        security=source.security,
        signs=source.signs,
        vps=source.vps,
        plywood=source.plywood,
        locked=source.locked,
    )
    new_pin.badges.set(source.badges.all())
    return new_pin


class PinShareDialogView(LoginRequiredMixin, View):
    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile=request.user.profile)
        return render(request, "dashboard/partials/pins/pin_share_dialog.html", {"pin": pin, "friends": get_connections(request.user.profile)})


class PinShareCreateView(LoginRequiredMixin, View):
    def post(self, request, pin_slug):
        sender = request.user.profile
        pin = get_object_or_404(Pin, slug=pin_slug, profile=sender)
        recipient = get_object_or_404(Profile, pk=request.POST.get("profile_id"))
        if recipient == sender or not are_connections(sender, recipient):
            return HttpResponse("Pins can only be shared with connected friends.", status=403)

        already_pinned = _recipient_has_pin(recipient, pin)
        share = PinShare.objects.create(
            pin=pin,
            from_profile=sender,
            to_profile=recipient,
            status=PinShareStatus.ALREADY_PINNED if already_pinned else PinShareStatus.PENDING,
        )
        notification = NotificationLog.objects.create(
            profile=recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
            message=(f"{sender.username} shared {pin.display_label} with you. You already have this location pinned." if already_pinned else f"{sender.username} shared {pin.display_label} with you."),
            url=reverse("pin.share.detail", kwargs={"share_id": share.id}),
        )
        share.notification = notification
        share.save(update_fields=["notification", "updated"])
        return render(request, "dashboard/partials/pins/pin_share_dialog.html", {"pin": pin, "friends": get_connections(sender), "shared_to": recipient})


class PinShareDetailView(LoginRequiredMixin, View):
    def get(self, request, share_id):
        share = get_object_or_404(PinShare.objects.select_related("pin__location", "from_profile__user", "to_profile"), pk=share_id, to_profile=request.user.profile)
        return render(request, "dashboard/pages/pin_share/detail.html", {"share": share, "pin": share.pin})


class PinShareRespondView(LoginRequiredMixin, View):
    def post(self, request, share_id):
        share = get_object_or_404(PinShare.objects.select_related("pin"), pk=share_id, to_profile=request.user.profile)
        action = request.POST.get("action")
        if share.status != PinShareStatus.PENDING:
            messages.info(request, "This shared pin has already been handled.")
            return redirect("pin.share.detail", share_id=share.id)
        if action == "accept":
            with transaction.atomic():
                if not _recipient_has_pin(share.to_profile, share.pin):
                    _create_pin_from_share(share)
                share.status = PinShareStatus.ACCEPTED
                share.save(update_fields=["status", "updated"])
            messages.success(request, "Pin added to your map.")
        elif action == "reject":
            share.status = PinShareStatus.REJECTED
            share.save(update_fields=["status", "updated"])
            messages.info(request, "Shared pin rejected.")
        if share.notification_id:
            NotificationLog.objects.filter(pk=share.notification_id).update(status=Status.READ)
        if request.headers.get("HX-Request"):
            from urbanlens.dashboard.controllers.notifications import _trigger_badge_refresh

            notifications = NotificationLog.objects.for_profile(request.user.profile).select_related("source_profile").order_by("-created")[:20]
            response = render(request, "dashboard/partials/notifications/notification_dropdown.html", {"notifications": notifications, "unread_count": NotificationLog.objects.for_profile(request.user.profile).unread().count()})
            return _trigger_badge_refresh(response)
        return redirect("pin.share.detail", share_id=share.id)
