"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    FriendshipController.py                                                                              *
*        Path:    /dashboard/controllers/friendship.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import FriendRequestVisibility, Profile, VisibilityChoice

logger = logging.getLogger(__name__)


def _friend_list_ctx(viewer: Profile | None, profile: Profile) -> dict:
    """Build context dict for friend list partials and pages.

    Determines:
    - friends: accepted friendship records for this profile
    - incoming_requests: pending requests TO this profile (only if viewer == profile)
    - viewer_friendship_status: status of the friendship between viewer and this profile
    - viewer_can_request: whether the viewer can send a friend request to this profile
    """
    friendships = (
        Friendship.objects.all()
        .profile(profile.pk)
        .is_friend()
        .select_related("from_profile__user", "to_profile__user")
    )

    friend_profiles: list[Profile] = []
    for f in friendships:
        friend_profiles.append(f.to_profile if f.from_profile_id == profile.pk else f.from_profile)

    incoming_requests: list[Friendship] = []
    viewer_friendship: Friendship | None = None
    viewer_can_request = False

    if viewer:
        # Incoming requests only shown to the profile owner
        if viewer.pk == profile.pk:
            incoming_requests = list(
                Friendship.objects.filter(
                    to_profile=profile,
                    status=FriendshipStatus.REQUESTED,
                ).select_related("from_profile__user"),
            )

        # Determine viewer's relationship with this profile
        if viewer.pk != profile.pk:
            try:
                viewer_friendship = Friendship.objects.all().between(viewer, profile)
            except Friendship.DoesNotExist:
                viewer_friendship = None

            status = viewer_friendship.status if viewer_friendship else None
            viewer_can_request = status is None or FriendshipStatus.can_request(status)

    return {
        "friends": friend_profiles,
        "incoming_requests": incoming_requests,
        "viewer_friendship": viewer_friendship,
        "viewer_can_request": viewer_can_request,
        "is_own_profile": viewer is not None and viewer.pk == profile.pk,
        "viewer": viewer,
        "friend_list_profile": profile,
    }


class FriendController(LoginRequiredMixin, GenericViewSet):
    def request_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        to_profile = Profile.objects.filter(pk=profile_id).first()
        if not to_profile:
            return HttpResponse("User not found.", status=404)

        requesting = request.user.profile
        visibility = to_profile.friend_request_visibility

        if visibility == FriendRequestVisibility.NO_ONE:
            return HttpResponse("This user is not accepting friend requests.", status=403)

        if visibility == FriendRequestVisibility.COMMON_PIN:
            from urbanlens.dashboard.models.pin.model import Pin
            req_locs = set(Pin.objects.filter(profile=requesting).exclude(location__isnull=True).values_list("location_id", flat=True))
            their_locs = set(Pin.objects.filter(profile=to_profile).exclude(location__isnull=True).values_list("location_id", flat=True))
            if not req_locs & their_locs:
                return HttpResponse("This user only accepts requests from people who share a pinned location.", status=403)

        elif visibility == FriendRequestVisibility.COMMON_FRIEND:
            req_friends = set(Friendship.objects.filter(from_profile=requesting, status=FriendshipStatus.ACCEPTED).values_list("to_profile_id", flat=True))
            req_friends |= set(Friendship.objects.filter(to_profile=requesting, status=FriendshipStatus.ACCEPTED).values_list("from_profile_id", flat=True))
            their_friends = set(Friendship.objects.filter(from_profile=to_profile, status=FriendshipStatus.ACCEPTED).values_list("to_profile_id", flat=True))
            their_friends |= set(Friendship.objects.filter(to_profile=to_profile, status=FriendshipStatus.ACCEPTED).values_list("from_profile_id", flat=True))
            if not req_friends & their_friends:
                return HttpResponse("This user only accepts requests from friends of friends.", status=403)

        elif visibility == FriendRequestVisibility.COMMON_TRIP:
            from urbanlens.dashboard.models.trips.model import TripMembership
            req_trips = set(TripMembership.objects.filter(profile=requesting).values_list("trip_id", flat=True))
            their_trips = set(TripMembership.objects.filter(profile=to_profile).values_list("trip_id", flat=True))
            if not req_trips & their_trips:
                return HttpResponse("This user only accepts requests from people on a shared trip.", status=403)

        friendship = Friendship.request(from_profile=requesting, to_profile=profile_id)
        if not friendship:
            return HttpResponse("Could not request friend.", status=400)

        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(requesting, to_profile),
        )

    def accept_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.accept()

        # Notify the original requester that their request was accepted.
        requester = friendship.from_profile if friendship.to_profile == request.user.profile else friendship.to_profile
        from django.urls import reverse
        NotificationLog.objects.create(
            profile=requester,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            title="Friend request accepted",
            message=f"{request.user.profile.username} accepted your friend request.",
            url=reverse("profile.view_user", kwargs={"profile_id": request.user.profile.pk}),
        )

        to_profile = Profile.objects.filter(pk=profile_id).first()
        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(request.user.profile, to_profile),
        )

    def reject_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.decline()
        to_profile = Profile.objects.filter(pk=profile_id).first()
        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(request.user.profile, to_profile),
        )

    def ignore_friend(self, request: HttpRequest, profile_id: int):
        """Ignore a friend request — no notification sent, button stays unavailable."""
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.ignore()
        to_profile = Profile.objects.filter(pk=profile_id).first()
        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(request.user.profile, to_profile),
        )

    def block_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            friendship = None

        if friendship:
            friendship.status = FriendshipStatus.BLOCKED
            friendship.save()
        else:
            other = Profile.objects.filter(pk=profile_id).first()
            if not other:
                return HttpResponse("Profile not found.", status=404)
            Friendship.objects.create(
                from_profile=request.user.profile,
                to_profile=other,
                status=FriendshipStatus.BLOCKED,
            )
        return HttpResponse("Profile blocked.")

    def mute_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.status = FriendshipStatus.MUTED
        friendship.save()
        return HttpResponse("Muted.")

    def remove_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.remove()
        to_profile = Profile.objects.filter(pk=profile_id).first()
        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(request.user.profile, to_profile),
        )

    def friend_list(self, request: HttpRequest, profile_id: int):
        """HTMX partial: friend list shown on the profile page."""
        profile = Profile.objects.filter(pk=profile_id).first()
        if not profile:
            return HttpResponse("")
        viewer = request.user.profile if request.user.is_authenticated else None
        return render(
            request,
            "dashboard/partials/friend_list_partial.html",
            _friend_list_ctx(viewer, profile),
        )

    def friends_page(self, request: HttpRequest, profile_id: int):
        """Full friends list page for a given profile."""
        profile = Profile.objects.filter(pk=profile_id).first()
        if not profile:
            from django.http import Http404
            raise Http404
        viewer = request.user.profile if request.user.is_authenticated else None
        return render(
            request,
            "dashboard/pages/profile/friends.html",
            {**_friend_list_ctx(viewer, profile), "profile": profile},
        )
