"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    ProfileController.py                                                                                 *
*        Path:    /dashboard/controllers/profile.py                                                                    *
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

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from urbanlens.dashboard.forms.profile import ProfileForm
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class ViewProfileView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _created = Profile.objects.get_or_create(user=request.user)
        return render(request, "dashboard/pages/profile/index.html", {"profile": profile})


class EditProfileView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _created = Profile.objects.get_or_create(user=request.user)
        form = ProfileForm(instance=profile)
        return render(request, "dashboard/pages/profile/edit.html", {"form": form})

    def post(self, request: HttpRequest) -> HttpResponse:
        profile: Profile = Profile.objects.get(user=request.user)
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            return redirect("view_profile")
        return render(request, "dashboard/pages/profile/edit.html", {"form": form})
