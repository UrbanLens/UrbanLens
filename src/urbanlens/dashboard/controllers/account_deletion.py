"""Self-service account deletion: request and cancel views for the settings page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from urbanlens.dashboard.forms.settings_form import DeleteAccountForm
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.account_deletion import cancel_deletion, request_deletion

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _safe_redirect_target(request: HttpRequest, default: str = "settings.view") -> HttpResponse:
    """Redirect to ``request.POST['next']`` if it's a safe same-site path, else to ``default``."""
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return HttpResponseRedirect(next_url)
    return redirect(default)


class RequestAccountDeletionView(LoginRequiredMixin, View):
    """Soft-deletes the current user's account after password + type-to-confirm verification."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        form = DeleteAccountForm(request.POST, user=request.user)
        if not form.is_valid():
            for error in form.errors.get("password", []) + form.errors.get("confirm_text", []):
                messages.error(request, str(error))
            return redirect("settings.view")

        request_deletion(profile)
        messages.success(
            request,
            f"Your account is scheduled for deletion on {profile.deletion_scheduled_for:%B %d, %Y}. You can undo this any time before then.",
        )
        return redirect("settings.view")


class CancelAccountDeletionView(LoginRequiredMixin, View):
    """Cancels a pending account deletion (the "undo" action on the warning banner)."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        cancel_deletion(profile)
        messages.success(request, "Account deletion cancelled. Your account is safe.")
        return _safe_redirect_target(request)
