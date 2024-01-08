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
from rest_framework import viewsets, status
from rest_framework.response import Response

from dashboard.models.profile.model import Profile
from dashboard.models.friendship.model import Friendship
from dashboard.models.friendship.serializer import FriendshipSerializer

class FriendshipViewSet(viewsets.ModelViewSet):
    queryset = Friendship.objects.all()
    serializer_class = FriendshipSerializer

    def create(self, request, *args, **kwargs):
        from_profile = Profile.objects.get(pk=request.data['from_profile'])
        to_profile = Profile.objects.get(pk=request.data['to_profile'])
        friendship = Friendship.objects.create(from_profile=from_profile, to_profile=to_profile, relationship_type=request.data['relationship_type'])
        friendship.save()
        return Response(status=status.HTTP_201_CREATED)
    
    def update(self, request, *args, **kwargs):
        friendship = self.get_object()
        friendship.status = request.data['status']
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
        return Friendship.objects.filter(from_profile__user=user)