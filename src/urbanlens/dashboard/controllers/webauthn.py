"""Passkey (WebAuthn) management for the Settings > Security section.

Registration is JSON/fetch-driven because the browser ceremony
(``navigator.credentials.create()``) has to run client-side between the
"begin" and "complete" calls - see ``webauthn-client.ts``. Renaming and
removing an existing passkey are plain server-rendered form posts like the
rest of the settings page, since neither needs another WebAuthn ceremony.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.account import WebAuthnCredential
from urbanlens.dashboard.services.two_factor import maybe_clear_backup_codes
from urbanlens.dashboard.services.webauthn import WebAuthnError, build_registration_options, verify_and_save_registration

if TYPE_CHECKING:
    from django.http import HttpRequest


class PasskeyRegisterOptionsView(LoginRequiredMixin, View):
    """POST: return WebAuthn registration options for the current user."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        try:
            options_json = build_registration_options(request, request.user)
        except WebAuthnError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return HttpResponse(options_json, content_type="application/json")


class PasskeyRegisterView(LoginRequiredMixin, View):
    """POST: verify a completed registration ceremony and save the new passkey."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        credential_json = request.POST.get("credential", "")
        name = request.POST.get("name", "")
        if not credential_json:
            return JsonResponse({"error": "Missing credential."}, status=400)

        try:
            credential = verify_and_save_registration(request, request.user, credential_json, name)
        except WebAuthnError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "name": credential.name}, status=201)


class PasskeyRenameView(LoginRequiredMixin, View):
    """POST: update a passkey's display name."""

    def post(self, request: HttpRequest, credential_id: int) -> HttpResponse:
        credential = get_object_or_404(WebAuthnCredential, pk=credential_id, user=request.user)
        name = request.POST.get("name", "").strip()[:100]
        credential.name = name or "Passkey"
        credential.save(update_fields=["name", "updated"])
        messages.success(request, "Passkey renamed.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class PasskeyDeleteView(LoginRequiredMixin, View):
    """POST: remove a passkey. Removing the last one turns 2FA back off."""

    def post(self, request: HttpRequest, credential_id: int) -> HttpResponse:
        credential = get_object_or_404(WebAuthnCredential, pk=credential_id, user=request.user)
        credential.delete()
        maybe_clear_backup_codes(credential.user)
        messages.success(request, "Passkey removed.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")
