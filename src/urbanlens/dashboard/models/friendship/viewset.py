"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    viewset.py                                                                                           *
*        Path:    /dashboard/models/friendship/viewset.py                                                              *
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

from rest_framework import status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.friendship.serializer import FriendshipSerializer
from urbanlens.dashboard.models.profile.model import Profile


class FriendshipViewSet(viewsets.ModelViewSet):
    queryset = Friendship.objects.all()
    serializer_class = FriendshipSerializer

    def create(self, request, *args, **kwargs):
        from_profile = Profile.objects.get(pk=request.data["from_profile"])
        to_profile = Profile.objects.get(pk=request.data["to_profile"])
        friendship = Friendship.objects.create(
            from_profile=from_profile, to_profile=to_profile, relationship_type=request.data["relationship_type"],
        )
        friendship.save()
        return Response(status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        friendship = self.get_object()
        friendship.status = request.data["status"]
        friendship.save()
        return Response(status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        friendship = self.get_object()
        friendship.delete()
        return Response(status=status.HTTP_200_OK)

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Friendship.objects.none()
        return Friendship.objects.all().filter(from_profile__user=user)
