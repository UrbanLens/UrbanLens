"""User settings controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from urbanlens.dashboard.forms.settings_form import PrivacySettingsForm
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class SettingsView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = {
            "privacy_form": PrivacySettingsForm(instance=profile),
        }
        return render(request, "dashboard/pages/settings/index.html", context)

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        privacy_form = PrivacySettingsForm(request.POST, instance=profile)
        if privacy_form.is_valid():
            privacy_form.save()
            return redirect("settings.view")
        context = {"privacy_form": privacy_form}
        return render(request, "dashboard/pages/settings/index.html", context)
