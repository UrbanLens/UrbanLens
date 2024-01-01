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
*        Path:    /ProfileController.py                                                                                *
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
from django.shortcuts import redirect, render
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from dashboard.forms.profile import ProfileForm
from dashboard.models.profile.model import Profile

class ViewProfileView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        profile, created = Profile.objects.get_or_create(user=request.user)
        return render(request, 'dashboard/pages/profile/view_profile.html', {'profile': profile})

class EditProfileView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        profile, created = Profile.objects.get_or_create(user=request.user)
        form = ProfileForm(instance=profile)
        return render(request, 'dashboard/pages/profile/edit_profile.html', {'form': form})

    def post(self, request, *args, **kwargs):
        profile = Profile.objects.get(user=request.user)
        form = ProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('view_profile')
        return render(request, 'dashboard/pages/profile/edit_profile.html', {'form': form})
