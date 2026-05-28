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
from urbanlens.dashboard.models.profile.model import FriendRequestVisibility, Profile

logger = logging.getLogger(__name__)


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

        return HttpResponse("Friend request sent.")

    def accept_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.accept()
        return HttpResponse("Friend request accepted.")

    def reject_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.reject()
        return HttpResponse("Friend request rejected.")

    def block_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        # If a friendship already exists
        friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        if friendship:
            friendship.block_friend()
            return HttpResponse("Relationship changed to blocked.")

        # If a friendship does not exist, create one with a status of blocked
        profile = Profile.objects.get(pk=profile_id)
        if not profile:
            return HttpResponse("Profile not found.", status=404)
        friendship = Friendship.objects.create(
            from_profile=request.user.profile,
            to_profile=profile,
            status=FriendshipStatus.BLOCKED,
        )
        friendship.save()
        return HttpResponse("Profile blocked.")

    def mute_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.mute()
        return HttpResponse("Friend request muted.")

    def remove_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.remove()
        return HttpResponse("Friend request removed.")

    def friend_list(self, request: HttpRequest, profile_id: int):
        friends = Friendship.objects.all().profile(profile_id)
        return render(request, "dashboard/pages/profile/view_friends.html", {"friends": friends})
