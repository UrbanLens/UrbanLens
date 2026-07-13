"""`@pin` / `@trip` / `@friend` sharing dialogs embedded in the messages page."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.controllers.direct_messages import _get_partner, _get_profile, _thread_context, _trigger_msg_badge_refresh
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.connections import get_connections
from urbanlens.dashboard.services.direct_message_shares import invite_to_trip_in_message, recommend_friend_in_message, share_pin_in_message

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class MessageSharePinView(LoginRequiredMixin, View):
    """GET/POST /messages/<profile_slug>/share/pin/ - share one of the sender's own pins in chat."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Render the pin-picker dialog body.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            The rendered dialog partial.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        pins = Pin.objects.filter(profile=profile, parent_pin__isnull=True).select_related("location").order_by("name")[:200]
        return render(request, "dashboard/partials/messages/_share_pin_dialog.html", {"partner": partner, "pins": pins})

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Create the PinShare + chat message and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``pin_slug``, ``body``, and
                optional ``markup_map_uuid``.
            profile_slug: Slug of the conversation partner.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        pin = get_object_or_404(Pin, slug=request.POST.get("pin_slug"), profile=profile)
        body = request.POST.get("body", "").strip() or f"Check out {pin.display_label}!"

        try:
            share_pin_in_message(profile, partner, pin, body, markup_map_uuid=request.POST.get("markup_map_uuid") or None)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)


def _get_share_message(profile: Profile, partner: Profile, message_id: int) -> DirectMessage:
    """Resolve a message-with-share for a respond action, scoped to this conversation.

    Args:
        profile: The requesting (viewing) profile - must be the message's recipient.
        partner: The conversation partner.
        message_id: PK of the message carrying the share.

    Returns:
        The message, with its `share` relation pre-selected.

    Raises:
        Http404: If no such message exists in this conversation, or it carries no share.
    """
    from django.http import Http404

    message = get_object_or_404(
        DirectMessage.objects.between(profile, partner).select_related(
            "share",
            "share__pin_share__pin__location",
            "share__recommended_profile",
        ),
        pk=message_id,
    )
    if message.recipient_id != profile.pk or getattr(message, "share", None) is None:
        raise Http404
    return message


def _toast_response(request: HttpRequest, template: str, context: dict, *, level: str, message: str) -> HttpResponse:
    """Render `template` and attach a `showToast` HX-Trigger.

    Args:
        request: The incoming request.
        template: Template path for the re-rendered fragment.
        context: Template context.
        level: Toast level (``success``, ``info``, ``warning``, ``error``).
        message: Toast text.

    Returns:
        The rendered response with the toast trigger attached.
    """
    response = render(request, template, context)
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


class MessageShareRespondPinView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/share/pin/<message_id>/respond/ - accept/reject a `@pin` share in place.

    Mirrors `PinShareRespondView` but stays inside the DM thread: no page
    navigation, buttons replaced by the resulting status, and a toast instead
    of a Django message (there's no next page load to carry it to).
    """

    def post(self, request: HttpRequest, profile_slug: str, message_id: int) -> HttpResponse:
        """Apply the accept/reject decision and return the refreshed share card.

        Args:
            request: The incoming request. Reads ``action`` (``accept``/``reject``).
            profile_slug: Slug of the conversation partner (the sharer).
            message_id: PK of the message carrying the pin share.

        Returns:
            The re-rendered `_message_share_card.html` fragment with a toast trigger.
        """
        from urbanlens.dashboard.controllers.pin_sharing import apply_pin_share_response

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        message = _get_share_message(profile, partner, message_id)
        share = message.share
        pin_share = share.pin_share
        if share.kind != "pin" or pin_share is None:
            return HttpResponseBadRequest("This share is not a pin share.")

        context = {"share": share, "viewer_id": profile.pk, "partner": partner}
        if pin_share.status != PinShareStatus.PENDING:
            return _toast_response(request, "dashboard/partials/messages/_message_share_card.html", context, level="info", message="This shared pin has already been handled.")

        action = request.POST.get("action")
        if action not in ("accept", "reject"):
            return HttpResponseBadRequest("Unknown action.")

        _target_pin, status_message = apply_pin_share_response(pin_share, action)
        return _toast_response(request, "dashboard/partials/messages/_message_share_card.html", context, level="success" if action == "accept" else "info", message=status_message)


class MessageShareRespondFriendView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/share/friend/<message_id>/respond/ - act on an `@friend` recommendation in place."""

    def post(self, request: HttpRequest, profile_slug: str, message_id: int) -> HttpResponse:
        """Send a friend request to the recommended profile and return the refreshed card.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner (the recommender).
            message_id: PK of the message carrying the recommendation.

        Returns:
            The re-rendered `_message_share_card.html` fragment with a toast trigger.
        """
        from urbanlens.dashboard.controllers.friendship import request_or_accept_friendship

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        message = _get_share_message(profile, partner, message_id)
        share = message.share
        recommended = share.recommended_profile
        if share.kind != "friend" or recommended is None:
            return HttpResponseBadRequest("This share is not a friend recommendation.")

        context = {"share": share, "viewer_id": profile.pk, "partner": partner}
        if not share.is_actionable:
            return _toast_response(request, "dashboard/partials/messages/_message_share_card.html", context, level="info", message="Already handled.")

        friendship = request_or_accept_friendship(profile, recommended)
        if not friendship:
            return HttpResponseBadRequest("Could not send friend request.")
        return _toast_response(request, "dashboard/partials/messages/_message_share_card.html", context, level="success", message=f"Friend request sent to {recommended.username}.")


class MessageShareTripView(LoginRequiredMixin, View):
    """GET/POST /messages/<profile_slug>/share/trip/ - invite the partner to a trip in chat."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Render the trip-picker dialog body.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            The rendered dialog partial.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        trips = Trip.objects.filter(memberships__profile=profile).exclude(memberships__profile=partner).distinct().order_by("-start_date")[:100]
        return render(request, "dashboard/partials/messages/_share_trip_dialog.html", {"partner": partner, "trips": trips})

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Create the trip invite + chat message and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``trip_slug`` and ``body``.
            profile_slug: Slug of the conversation partner.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        trip = get_object_or_404(Trip, slug=request.POST.get("trip_slug"))
        body = request.POST.get("body", "").strip() or f'I invited you to "{trip.name}"!'

        try:
            invite_to_trip_in_message(profile, partner, trip, body)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)


class MessageShareFriendView(LoginRequiredMixin, View):
    """GET/POST /messages/<profile_slug>/share/friend/ - recommend one of the sender's friends in chat."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Render the friend-picker dialog body.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            The rendered dialog partial.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        friends = [f for f in get_connections(profile) if f.pk != partner.pk]
        return render(request, "dashboard/partials/messages/_share_friend_dialog.html", {"partner": partner, "friends": friends})

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Create the friend recommendation + chat message and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``recommended_slug`` and ``body``.
            profile_slug: Slug of the conversation partner.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        recommended = get_object_or_404(Profile, slug=request.POST.get("recommended_slug"))
        body = request.POST.get("body", "").strip() or f"I think you and {recommended.username} should connect!"

        try:
            recommend_friend_in_message(profile, partner, recommended, body)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)
