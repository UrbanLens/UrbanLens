"""API key management for the Settings > Security section.

Creation and revocation are plain server-rendered form posts, like passkey
rename/delete - there's no client-side ceremony involved, just generate (or
revoke) and show the result. On an htmx request the whole Security section
re-renders in place (the same swap target the TOTP/backup-codes subsections
use, see ``two_factor.py``) so a freshly-created key's one-time plaintext
reveal shows up without a full page navigation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View

# two_factor owns the shared Security-section partial rendering (_security_section_response
# already merges in the API-keys context) - reuse it rather than duplicating the
# context-assembly logic here, so the two subsections can't drift apart.
from urbanlens.dashboard.controllers.two_factor import _is_htmx, _security_section_response
from urbanlens.dashboard.services.api_keys import generate_api_key, revoke_api_key

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class ApiKeyCreateView(LoginRequiredMixin, View):
    """POST: generate a new API key and reveal its plaintext exactly once."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        name = request.POST.get("name", "")
        _api_key, raw_key = generate_api_key(request.user, name)
        # One-time display: the next render (this one, or the full settings
        # page's) pops this from the session - it can never be shown again.
        request.session["new_api_key"] = raw_key
        if _is_htmx(request):
            return _security_section_response(request, request.user)
        return redirect(f"{reverse('settings.view')}#api-keys-settings-section")


class ApiKeyRevokeView(LoginRequiredMixin, View):
    """POST: revoke one of the user's API keys. Immediate and irreversible."""

    def post(self, request: HttpRequest, api_key_id: int) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        revoke_api_key(request.user, api_key_id)
        if _is_htmx(request):
            return _security_section_response(request, request.user)
        messages.success(request, "API key revoked.")
        return redirect(f"{reverse('settings.view')}#api-keys-settings-section")
