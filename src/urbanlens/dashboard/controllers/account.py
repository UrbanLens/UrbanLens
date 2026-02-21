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
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-31                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-31     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import generic
from social_core.exceptions import MissingBackend
from social_django.utils import load_backend, load_strategy

from urbanlens.dashboard.models.profile import Profile


class SignupView(generic.CreateView):
    form_class = UserCreationForm
    success_url = reverse_lazy("login")
    template_name = "registration/signup.html"


def social_auth(request: HttpRequest, backend) -> HttpResponseRedirect:
    strategy = load_strategy(request)
    try:
        backend = load_backend(strategy=strategy, name=backend, redirect_uri=None)
    except MissingBackend:
        return redirect("signup")

    user = backend.complete_user_authentication(request)
    if user and user.is_active:
        # Create a profile for the user
        Profile.objects.get_or_create(user=user)

        login(request, user)
        return redirect("home")
    return redirect("signup")
