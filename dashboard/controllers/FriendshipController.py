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
*        Path:    /FriendshipController.py                                                                             *
*        Project: controllers                                                                                          *
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
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.contrib.auth.models import User
from dashboard.models.friendship.model import Friendship

@login_required
def request_friend(request):
    if request.method == 'POST':
        friend_username = request.POST.get('friend_username')
        friend = User.objects.get(username=friend_username)
        Friendship.objects.create(user=request.user, friend=friend)
        return HttpResponse('Friend request sent.')

@login_required
def list_friends(request):
    friends = Friendship.objects.filter(user=request.user)
    return render(request, 'dashboard/pages/profile/view_friends.html', {'friends': friends})
