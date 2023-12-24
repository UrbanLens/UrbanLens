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
*        Path:    /viewset.py                                                                                          *
*        Project: friendship                                                                                           *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations
from rest_framework import viewsets, status
from rest_framework.response import Response

from dashboard.models.profile.model import Profile
from dashboard.models.friendship.model import Friendship
from dashboard.models.friendship.serializer import FriendshipSerializer

class FriendshipViewSet(viewsets.ModelViewSet):
    queryset = Friendship.objects.all()
    serializer_class = FriendshipSerializer

    def create(self, request, *args, **kwargs):
        friend = Profile.objects.get(id=request.data.get('friend_id'))
        if Friendship.objects.filter(user=request.user, friend=friend).exists():
            return Response({"detail": "Friendship already exists"}, status=status.HTTP_400_BAD_REQUEST)
        Friendship.objects.create(user=request.user, friend=friend)
        return Response({"detail": "Friendship created"}, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        friend = Profile.objects.get(id=request.data.get('friend_id'))
        friendship = Friendship.objects.filter(user=request.user, friend=friend)
        if not friendship.exists():
            return Response({"detail": "Friendship does not exist"}, status=status.HTTP_400_BAD_REQUEST)
        friendship.delete()
        return Response({"detail": "Friendship deleted"}, status=status.HTTP_204_NO_CONTENT)
