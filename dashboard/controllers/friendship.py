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
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from django.shortcuts import render
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.contrib.auth.models import User
from dashboard.models.friendship.model import Friendship

class RequestFriendView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        friend_username = request.POST.get('friend_username')
        friend = User.objects.get(username=friend_username)
        Friendship.objects.create(user=request.user, friend=friend)
        return HttpResponse('Friend request sent.')

class ListFriendsView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        friends = Friendship.objects.filter(user=request.user)
        return render(request, 'dashboard/pages/profile/view_friends.html', {'friends': friends})
