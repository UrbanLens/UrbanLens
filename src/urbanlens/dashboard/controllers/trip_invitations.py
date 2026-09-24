"""Trip invitations sent to an email address: the invitee's response page and the inviter's cancel action."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.invitation import TripInvitationResponse
from urbanlens.dashboard.services.trips.trip_errors import TripError, TripNotFoundError
from urbanlens.dashboard.services.trips.trip_invitations import (
    cancel_invitation,
    friendship_offer_open,
    invitation_for_token,
    respond_to_friendship,
    respond_to_trip,
)

if TYPE_CHECKING:
    import uuid

    from django.http import HttpRequest

    from urbanlens.dashboard.models.trips.invitation import TripInvitation


def _not_found(request: HttpRequest) -> HttpResponse:
    return render(request, "dashboard/pages/trips/invitation_not_found.html", status=404)


def _other_address(request: HttpRequest) -> HttpResponse:
    return render(request, "dashboard/pages/invitation_other_address.html", status=403)


def _wants_yes(request: HttpRequest) -> bool:
    return request.POST.get("answer") == "accept"


class TripInvitationView(View):
    """The invitee's page: join the trip, and become friends with the inviter, as separate answers.

    GET /trips/invitations/<token>/
    """

    def get(self, request: HttpRequest, token: uuid.UUID) -> HttpResponse:
        try:
            invitation = invitation_for_token(token)
        except TripError:
            return _not_found(request)

        profile = Profile.objects.filter(user=request.user).first() if request.user.is_authenticated else None
        if profile is not None and not invitation.addressed_to(profile):
            return _other_address(request)
        return render(request, "dashboard/pages/trips/invitation.html", _page_context(invitation, profile))


def _page_context(invitation: TripInvitation, profile: Profile | None) -> dict:
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    inviter_name = resolve_visible_identity(profile, invitation.inviter)["display_name"] if profile is not None else invitation.inviter.username
    return {
        "invitation": invitation,
        "trip": invitation.trip,
        "inviter_name": inviter_name,
        "profile": profile,
        "trip_open": invitation.is_open(),
        "trip_joined": invitation.trip_response == TripInvitationResponse.ACCEPTED,
        "friend_open": profile is not None and friendship_offer_open(invitation, profile),
        "friend_accepted": invitation.friend_response == TripInvitationResponse.ACCEPTED,
        "signup_url": f"{reverse('signup')}?invite={invitation.token}",
        "login_url": f"{reverse('login')}?next={reverse('trips.invitation', kwargs={'token': invitation.token})}",
    }


class _AnswerView(View):
    """Shared shape of the two signed-in answers: resolve the token, apply, return to the page."""

    def post(self, request: HttpRequest, token: uuid.UUID) -> HttpResponse:
        page = reverse("trips.invitation", kwargs={"token": token})
        if not request.user.is_authenticated:
            return redirect_to_login(page)
        try:
            invitation = invitation_for_token(token)
        except TripError:
            return _not_found(request)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not invitation.addressed_to(profile):
            return _other_address(request)
        try:
            self.answer(invitation, profile, accept=_wants_yes(request))
        except TripNotFoundError:
            return _not_found(request)
        except TripError as exc:
            messages.error(request, exc.message)
            return redirect(page)
        if _wants_yes(request) and isinstance(self, TripInvitationTripAnswerView):
            return redirect("trips.detail", trip_slug=invitation.trip.slug)
        return redirect(page)

    def answer(self, invitation: TripInvitation, profile: Profile, *, accept: bool) -> None:
        raise NotImplementedError


class TripInvitationTripAnswerView(_AnswerView):
    """POST /trips/invitations/<token>/trip/ - join or decline the trip."""

    def answer(self, invitation: TripInvitation, profile: Profile, *, accept: bool) -> None:
        respond_to_trip(invitation, profile, accept=accept)
        messages.success(self.request, "You joined the trip." if accept else "You declined the trip.")


class TripInvitationFriendAnswerView(_AnswerView):
    """POST /trips/invitations/<token>/friend/ - become friends with the inviter, or decline."""

    def answer(self, invitation: TripInvitation, profile: Profile, *, accept: bool) -> None:
        respond_to_friendship(invitation, profile, accept=accept)
        messages.success(self.request, "You're now friends." if accept else "You declined the friend request.")


class TripInvitationCancelView(LoginRequiredMixin, View):
    """POST /trips/<slug>/invitations/<uuid>/cancel/ - the inviter withdraws an open invitation."""

    def post(self, request: HttpRequest, trip_slug: str, invitation_uuid: uuid.UUID) -> HttpResponse:
        from urbanlens.dashboard.controllers.trip import _render_members_panel, _trip_error_response, trip_or_not_found

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        try:
            cancel_invitation(result, profile, invitation_uuid)
        except TripError as exc:
            return _trip_error_response(exc)
        return _render_members_panel(request, result, profile)
