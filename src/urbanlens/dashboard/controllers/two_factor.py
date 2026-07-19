"""TOTP (authenticator app) enrollment and backup-code management for Settings > Security.

Unlike passkey registration, none of this needs browser-side WebAuthn ceremony
JavaScript - an authenticator app just needs to scan a QR code and the user
types back a 6-digit code. Every view here is still a plain server-rendered
form POST that works with no JS at all (progressive enhancement): on success
or failure it redirects back to the settings page. When the request carries
an ``HX-Request`` header (the forms in ``_security_section_body.html`` all
do), it instead re-renders just that partial in place, so the section
updates without a full page navigation.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
import qrcode

from urbanlens.dashboard.services.api_keys import api_keys_settings_context
from urbanlens.dashboard.services.two_factor import (
    SESSION_PENDING_TOTP_SECRET,
    disable_totp,
    enroll_totp,
    generate_backup_codes,
    generate_totp_secret,
    has_second_factor,
    security_settings_context,
    totp_provisioning_uri,
    verify_totp_setup_code,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

_SECURITY_SECTION_PARTIAL = "dashboard/partials/settings/_security_section_body.html"


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _security_section_response(request: HttpRequest, user: User, **extra: object) -> HttpResponse:
    """Render the Security section partial for an htmx swap, with fresh state.

    Merges in the API Keys subsection's context too - both live in the same
    partial and share the same swap target, so every action that re-renders
    one must re-render the other with equally fresh state.
    """
    context = {**security_settings_context(user, request), **api_keys_settings_context(user, request), **extra}
    return render(request, _SECURITY_SECTION_PARTIAL, context)


class TOTPSetupStartView(LoginRequiredMixin, View):
    """POST: generate a pending TOTP secret and stash it in the session for confirmation."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        request.session[SESSION_PENDING_TOTP_SECRET] = generate_totp_secret()
        if _is_htmx(request):
            return _security_section_response(request, request.user)
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
            if _is_htmx(request):
                return _security_section_response(request, request.user, code_error="That setup session expired. Please start again.")
            messages.error(request, "That setup session expired. Please start again.")
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        if not verify_totp_setup_code(secret, code):
            if _is_htmx(request):
                return _security_section_response(request, request.user, code_error="That code didn't match. Please try again.")
            messages.error(request, "That code didn't match. Please try again.")
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        enroll_totp(request.user, secret)
        del request.session[SESSION_PENDING_TOTP_SECRET]
        if _is_htmx(request):
            return _security_section_response(request, request.user)
        messages.success(request, "Authenticator app added.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class TOTPSetupCancelView(LoginRequiredMixin, View):
    """POST: abandon a pending (unconfirmed) TOTP setup."""

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(SESSION_PENDING_TOTP_SECRET, None)
        if _is_htmx(request) and isinstance(request.user, User):
            return _security_section_response(request, request.user)
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class TOTPDisableView(LoginRequiredMixin, View):
    """POST: remove the account's TOTP device."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        disable_totp(request.user)
        if _is_htmx(request):
            return _security_section_response(request, request.user)
        messages.success(request, "Authenticator app removed.")
        return redirect(f"{reverse('settings.view')}#security-settings-section")


class BackupCodesGenerateView(LoginRequiredMixin, View):
    """POST: (re)generate backup codes, requiring at least one other factor first."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return redirect("login")
        if not has_second_factor(request.user):
            message = "Set up a passkey or authenticator app before generating backup codes."
            if _is_htmx(request):
                return _security_section_response(request, request.user, backup_codes_error=message)
            messages.error(request, message)
            return redirect(f"{reverse('settings.view')}#security-settings-section")

        codes = generate_backup_codes(request.user)
        # One-time display: SettingsView.get() (or the htmx partial below) pops
        # this on the very next render.
        request.session["new_backup_codes"] = codes
        if _is_htmx(request):
            return _security_section_response(request, request.user)
        return redirect(f"{reverse('settings.view')}#security-settings-section")
