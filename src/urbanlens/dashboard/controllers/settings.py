"""User settings controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from urbanlens.dashboard.forms.settings_form import ContactSettingsForm, PrivacySettingsForm, StyleSettingsForm
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class SettingsView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = {
            "privacy_form": PrivacySettingsForm(instance=profile),
            "contact_form": ContactSettingsForm(initial={"email": request.user.email}),
            "style_form": StyleSettingsForm(instance=profile),
        }
        return render(request, "dashboard/pages/settings/index.html", context)

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        section = request.POST.get("section")

        privacy_form = PrivacySettingsForm(instance=profile)
        contact_form = ContactSettingsForm(initial={"email": request.user.email})
        style_form = StyleSettingsForm(instance=profile)

        if section == "privacy":
            privacy_form = PrivacySettingsForm(request.POST, instance=profile)
            if privacy_form.is_valid():
                privacy_form.save()
                messages.success(request, "Privacy settings saved.")
                return redirect("settings.view")

        elif section == "contact":
            contact_form = ContactSettingsForm(request.POST)
            if contact_form.is_valid():
                request.user.email = contact_form.cleaned_data["email"]
                request.user.save(update_fields=["email"])
                messages.success(request, "Contact settings saved.")
                return redirect("settings.view")

        elif section == "style":
            style_form = StyleSettingsForm(request.POST, instance=profile)
            if style_form.is_valid():
                style_form.save()
                messages.success(request, "Style settings saved.")
                return redirect("settings.view")

        context = {
            "privacy_form": privacy_form,
            "contact_form": contact_form,
            "style_form": style_form,
        }
        return render(request, "dashboard/pages/settings/index.html", context)
