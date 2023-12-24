from django.shortcuts import render
from django.contrib.auth.decorators import login_required
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
    return render(request, 'dashboard/view_friends.html', {'friends': friends})
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
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
    return render(request, 'dashboard/view_friends.html', {'friends': friends})
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
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
    return render(request, 'dashboard/view_friends.html', {'friends': friends})
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from dashboard.models.friendship.model import Friendship
from django.http import HttpResponse
from django.contrib.auth.models import User

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
    return render(request, 'dashboard/view_friends.html', {'friends': friends})
