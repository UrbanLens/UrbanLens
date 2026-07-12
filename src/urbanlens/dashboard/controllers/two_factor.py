"""TOTP (authenticator app) enrollment and backup-code management for Settings > Security.

Unlike passkey registration, none of this needs browser-side JavaScript - an
authenticator app just needs to scan a QR code and the user types back a
6-digit code, so every view here is a plain server-rendered form POST/GET,
consistent with the rest of the settings page.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
import qrcode

from urbanlens.dashboard.services.two_factor import (
    SESSION_PENDING_TOTP_SECRET,
    disable_totp,
    enroll_totp,
    generate_backup_codes,
    generate_totp_secret,
    has_second_factor,
    totp_provisioning_uri,
    verify_totp_setup_code,
)

if TYPE_CHECKING:
    from django.http import HttpRequest


class TOTPSetupStartView(LoginRequiredMixin, View):
    """POST: generate a pending TOTP secret and stash it in the session for confirmation."""

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session[SESSION_PENDING_TOTP_SECRET] = generate_totp_secret()
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class TOTPQRCodeView(LoginRequiredMixin, View):
    """GET: render the pending secret's QR code as a PNG.

    Reads the secret from the session rather than a URL parameter so it
    never appears in server logs, browser history, or a Referer header.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            raise Http404
        secret = request.session.get(SESSION_PENDING_TOTP_SECRET)
        if not secret:
            raise Http404
        uri = totp_provisioning_uri(request.user, secret)
        buffer = io.BytesIO()
        qrcode.make(uri).save(buffer, format="PNG")
        return HttpResponse(buffer.getvalue(), content_type="image/png")


class TOTPSetupConfirmView(LoginRequiredMixin, View):
    """POST: verify a code against the pending secret and, on success, enable TOTP."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        secret = request.session.get(SESSION_PENDING_TOTP_SECRET)
        code = request.POST.get("code", "")
        if not secret:
            messages.error(request, "That setup session expired. Please start again.")
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        if not verify_totp_setup_code(secret, code):
            messages.error(request, "That code didn't match. Please try again.")
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        enroll_totp(request.user, secret)
        del request.session[SESSION_PENDING_TOTP_SECRET]
        messages.success(request, "Authenticator app added.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class TOTPSetupCancelView(LoginRequiredMixin, View):
    """POST: abandon a pending (unconfirmed) TOTP setup."""

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(SESSION_PENDING_TOTP_SECRET, None)
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class TOTPDisableView(LoginRequiredMixin, View):
    """POST: remove the account's TOTP device."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        disable_totp(request.user)
        messages.success(request, "Authenticator app removed.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class BackupCodesGenerateView(LoginRequiredMixin, View):
    """POST: (re)generate backup codes, requiring at least one other factor first."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        if not has_second_factor(request.user):
            messages.error(request, "Set up a passkey or authenticator app before generating backup codes.")
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        codes = generate_backup_codes(request.user)
        # One-time display: SettingsView.get() pops this on the very next render.
        request.session["new_backup_codes"] = codes
        return redirect(f"{reverse('settings.view')}#security-settings-section")
