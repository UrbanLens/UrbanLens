"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    account.py                                                                                           *
*        Path:    /dashboard/controllers/account.py                                                                    *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-31                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-31     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from django.contrib.auth.forms import UserCreationForm
from django.urls import reverse_lazy
from django.views import generic
from django.shortcuts import redirect
from social_django.utils import load_strategy, load_backend
from social_core.exceptions import MissingBackend
from django.contrib.auth import login

from dashboard.models.profile import Profile

class SignupView(generic.CreateView):
    form_class = UserCreationForm
    success_url = reverse_lazy('login')
    template_name = 'registration/signup.html'

def social_auth(request, backend):
    strategy = load_strategy(request)
    try:
        backend = load_backend(strategy=strategy, name=backend, redirect_uri=None)
    except MissingBackend:
        return redirect('signup')

    user = backend.complete_user_authentication(request)
    if user and user.is_active:
        # Create a profile for the user
        Profile.objects.get_or_create(user=user)

        login(request, user)
        return redirect('home')
    else:
        return redirect('signup')
