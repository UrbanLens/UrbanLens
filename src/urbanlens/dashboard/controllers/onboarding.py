"""First-login welcome page: bulk Memories/Community/External-APIs toggles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from urbanlens.dashboard.forms.onboarding_form import WelcomeOnboardingForm
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.community import bulk_privatize_pins

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class WelcomeOnboardingView(LoginRequiredMixin, View):
    """Shown once, on first login, before any other onboarding step.

    GET  /welcome/  -> render the toggle form (all pre-checked).
    POST /welcome/  -> save the toggles and mark onboarding complete.

    Redirects back through ``post_login`` rather than straight to
    ``profile.edit``/``map.view`` so PostLoginRedirectView's existing
    decision chain (site-admin setup, then the username/avatar setup banner,
    then the map) runs exactly once, in one place.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if profile.welcome_onboarding_complete:
            return redirect("post_login")
        return render(request, "dashboard/pages/onboarding/welcome.html", {"form": WelcomeOnboardingForm(instance=profile)})

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        was_community_enabled = profile.community_enabled
        form = WelcomeOnboardingForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            if was_community_enabled and not profile.community_enabled:
                bulk_privatize_pins(profile)
            profile.welcome_onboarding_complete = True
            profile.save(update_fields=["welcome_onboarding_complete"])
            return redirect("post_login")
        return render(request, "dashboard/pages/onboarding/welcome.html", {"form": form})
