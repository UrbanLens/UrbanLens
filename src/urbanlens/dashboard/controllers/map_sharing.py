"""Controllers for sharing a standalone MarkupMap with one friend.

Modeled directly on ``controllers.pin_sharing`` - the standalone-dialog half
of that module, not the simpler ``services.pin_sharing`` core - since this is
likewise reached as its own action (from Memories > Maps) rather than folded
into another flow. Unlike PinShare there is no accept/reject step: the
recipient's only action is viewing the map and optionally cloning it via
"Add to my maps" (see ``controllers.markup.MarkupMapCloneView``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.connections import are_connections, get_connections
from urbanlens.dashboard.services.map_sharing import share_markup_map_with_profile
from urbanlens.dashboard.services.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH, text_length_error

if TYPE_CHECKING:
    from django.http import HttpRequest


class MarkupMapShareDialogView(LoginRequiredMixin, View):
    """GET /markup-maps/<uuid:map_uuid>/share/ - friend-picker dialog."""

    def get(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Render the friend-picker dialog for sharing one of the caller's own maps.

        Args:
            request: HttpRequest.
            map_uuid: UUID of the map to share.

        Returns:
            Rendered dialog HTML.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile=profile)
        return render(
            request,
            "dashboard/partials/map/_markup_map_share_dialog.html",
            {"map": markup_map, "friends": get_connections(profile)},
        )


class MarkupMapShareCreateView(LoginRequiredMixin, View):
    """POST /markup-maps/<uuid:map_uuid>/share/send/"""

    def post(self, request: HttpRequest, map_uuid: str) -> HttpResponse:
        """Share the caller's map with a connected friend.

        Args:
            request: HttpRequest with ``profile_id`` and optional ``message``.
            map_uuid: UUID of the map to share.

        Returns:
            Rendered dialog HTML confirming the share, or a 400/403 on error.
        """
        sender, _ = Profile.objects.get_or_create(user=request.user)
        markup_map = get_object_or_404(MarkupMap, uuid=map_uuid, profile=sender)
        recipient = get_object_or_404(Profile, pk=request.POST.get("profile_id"))
        if recipient == sender or not are_connections(sender, recipient):
            return HttpResponse("Maps can only be shared with connected friends.", status=403)

        message = (request.POST.get("message") or "").strip() or None
        length_error = text_length_error(message, MAX_PIN_SHARE_MESSAGE_LENGTH, "Message")
        if length_error:
            return HttpResponse(length_error, status=400)

        share = MarkupMapShare.objects.create(markup_map=markup_map, from_profile=sender, to_profile=recipient, message=message)
        notification = NotificationLog.objects.create(
            profile=recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.MAP_SHARED,
            title="Map shared with you",
            message=f"{sender.username} shared a map with you.",
            url=reverse("markup_map.share.detail", kwargs={"share_id": share.pk}),
        )
        share.notification = notification
        share.save(update_fields=["notification", "updated"])

        share_markup_map_with_profile(sender, recipient, markup_map)

        return render(
            request,
            "dashboard/partials/map/_markup_map_share_dialog.html",
            {"map": markup_map, "friends": get_connections(sender), "shared_to": recipient},
        )


class MarkupMapShareDetailView(LoginRequiredMixin, View):
    """GET /map-shares/<int:share_id>/ - the recipient's view of a shared map."""

    def get(self, request: HttpRequest, share_id: int) -> HttpResponse:
        """Render the shared-map detail page for its recipient.

        Args:
            request: HttpRequest.
            share_id: PK of the MarkupMapShare.

        Returns:
            Rendered detail page, scoped to the share's recipient.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        share = get_object_or_404(
            MarkupMapShare.objects.select_related("markup_map", "from_profile__user", "to_profile").prefetch_related("markup_map__items"),
            pk=share_id,
            to_profile=profile,
        )
        return render(request, "dashboard/pages/map_share/detail.html", {"share": share, "map": share.markup_map})
