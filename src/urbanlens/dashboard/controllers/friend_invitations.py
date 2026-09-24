"""The invitee's page for a friend invitation sent to their email address."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social.friend_invitations import (
    FriendInvitationError,
    accept,
    addressed_to,
    decline,
    invitation_for_token,
    is_open_for,
)

if TYPE_CHECKING:
    import uuid

    from django.http import HttpRequest, HttpResponse


def _not_found(request: HttpRequest) -> HttpResponse:
    return render(request, "dashboard/pages/trips/invitation_not_found.html", status=404)


def _viewer(request: HttpRequest) -> Profile | None:
    return Profile.objects.filter(user=request.user).first() if request.user.is_authenticated else None


class FriendInvitationView(View):
    """GET /friendship/invitations/<token>/ - accept or decline, as a separate choice from signing up."""

    def get(self, request: HttpRequest, token: uuid.UUID) -> HttpResponse:
        invitation = invitation_for_token(token)
        profile = _viewer(request)
        if invitation is None or (profile is not None and not addressed_to(invitation, profile)):
            return _not_found(request)
        page = reverse("friend.invitation", kwargs={"token": invitation.token})
        return render(
            request,
            "dashboard/pages/friends/invitation.html",
            {
                "invitation": invitation,
                "inviter": invitation.inviter,
                "profile": profile,
                "is_open": profile is not None and is_open_for(invitation, profile),
                "unanswered": invitation.accepted_at is None and invitation.declined_at is None,
                "signup_url": f"{reverse('signup')}?invite={invitation.token}",
                "login_url": f"{reverse('login')}?next={page}",
            },
        )


class FriendInvitationAnswerView(View):
    """POST /friendship/invitations/<token>/answer/ - ``answer=accept`` needs an account; declining does not."""

    def post(self, request: HttpRequest, token: uuid.UUID) -> HttpResponse:
        invitation = invitation_for_token(token)
        if invitation is None:
            return _not_found(request)
        page = reverse("friend.invitation", kwargs={"token": invitation.token})
        profile = _viewer(request)
        wants_friendship = request.POST.get("answer") == "accept"
        if wants_friendship and profile is None:
            return redirect_to_login(page)
        if profile is not None and not addressed_to(invitation, profile):
            return _not_found(request)
        try:
            if wants_friendship and profile is not None:
                accept(invitation, profile)
                messages.success(request, "You're now friends.")
            else:
                decline(invitation, profile)
                messages.success(request, "You declined the invitation.")
        except FriendInvitationError as exc:
            messages.error(request, str(exc))
        return redirect(page)
