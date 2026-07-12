"""`@pin` / `@trip` / `@friend` sharing dialogs embedded in the messages page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.controllers.direct_messages import _get_partner, _get_profile, _thread_context, _trigger_msg_badge_refresh
from urbanlens.dashboard.models.pin.model import Pin
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
            request: The incoming request. Reads ``trip_uuid`` and ``body``.
            profile_slug: Slug of the conversation partner.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        trip = get_object_or_404(Trip, uuid=request.POST.get("trip_uuid"))
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
