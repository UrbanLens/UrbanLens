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
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
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
from django.shortcuts import render
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from rest_framework.viewsets import GenericViewSet

from dashboard.models.friendship.model import Friendship
from dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

class FriendController(LoginRequiredMixin, GenericViewSet):

    def request_friend(self, request, profile_id : int, *args, **kwargs):
        friendship = Friendship.request(from_profile=request.user.profile, to_profile=profile_id)
        if not friendship:
            return HttpResponse('Could not request friend.', status=400)

        return HttpResponse('Friend request sent.')

    def accept_friend(self, request, profile_id : int, *args, **kwargs):
        friendship = Friendship.objects.between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse('Friend request not found.', status=404)

        friendship.accept()
        return HttpResponse('Friend request accepted.')

    def reject_friend(self, request, profile_id : int, *args, **kwargs):
        friendship = Friendship.objects.between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse('Friend request not found.', status=404)

        friendship.reject()
        return HttpResponse('Friend request rejected.')

    def block_friend(self, request, profile_id : int, *args, **kwargs):
        # If a friendship already exists
        friendship = Friendship.objects.between(profile_id, request.user.profile)
        if friendship:
            friendship.block_friend()
            return HttpResponse('Relationship changed to blocked.')

        # If a friendship does not exist, create one with a status of blocked
        profile = Profile.objects.get(pk=profile_id)
        if not profile:
            return HttpResponse('Profile not found.', status=404)
        friendship = Friendship.objects.create(from_profile=request.user.profile, to_profile=profile, relationship_type=Friendship.FriendshipStatus.BLOCKED)
        friendship.save()
        return HttpResponse('Profile blocked.')

    def mute_friend(self, request, profile_id : int, *args, **kwargs):
        friendship = Friendship.objects.between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse('Friend request not found.', status=404)

        friendship.mute()
        return HttpResponse('Friend request muted.')

    def remove_friend(self, request, profile_id : int, *args, **kwargs):
        friendship = Friendship.objects.between(profile_id, request.user.profile)
        if not friendship:
            return HttpResponse('Friend request not found.', status=404)

        friendship.remove()
        return HttpResponse('Friend request removed.')

    def friend_list(self, request, profile_id, *args, **kwargs):
        friends = Friendship.objects.profile(profile_id)
        return render(request, 'dashboard/pages/profile/view_friends.html', {'friends': friends})
