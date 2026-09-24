"""Auth controllers: registration, email verification, login."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django import forms

# Aliased: several functions here bind a local `settings` to SiteSettings.
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login as auth_login, views as auth_views
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, SetPasswordForm, UserCreationForm, UsernameField
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View, generic
from django.views.decorators.http import require_GET, require_POST

from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.services.admin.site_admin import should_redirect_to_site_admin
from urbanlens.dashboard.services.auth.two_factor import SESSION_WEBAUTHN_PENDING_REDIRECT as _WEBAUTHN_PENDING_REDIRECT_KEY, SESSION_WEBAUTHN_PENDING_USER as _WEBAUTHN_PENDING_USER_KEY
from urbanlens.dashboard.services.auth.username import USERNAME_RULES, USERNAME_UNAVAILABLE, username_is_available
from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import Outage
from urbanlens.dashboard.services.security.client_ip import client_ip
from urbanlens.dashboard.services.security.throttle import Rate

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Lockout counters keep counting in-process while the cache is down: refusing
#: every login would turn a cache outage into a site outage, and not counting
#: would hand an attacker the outage as an unmetered window.
_LOCKOUT_OUTAGE = Outage.LOCAL


# -- Login rate limiting helpers ------------------------------------------------


def _attempts_key(key: str) -> str:
    """Cache key for the failed-attempt counter for a given lockout key."""
    return f"login_attempts:{key}"


def _lockout_key(key: str) -> str:
    """Cache key for the lockout flag for a given lockout key."""
    return f"login_lockout:{key}"


def _is_locked_out(key: str) -> bool:
    """Return True if ``key`` is currently locked out."""
    return counters.peek(_lockout_key(key), on_outage=_LOCKOUT_OUTAGE) > 0


def _bump_counter(key: str, timeout: int) -> int:
    """Count one failure and return the total, living *timeout* seconds past the last one.

    Args:
        key: The counter's cache key.
        timeout: Seconds the counter should survive its last increment.

    Returns:
        The failure count including this one.
    """
    return counters.hit(key, timeout, on_outage=_LOCKOUT_OUTAGE, sliding=True)


def _set_lockout(key: str, seconds: int) -> None:
    """Raise a lockout flag for *seconds*; a flag is a counter that is read as "above zero"."""
    counters.hit(key, seconds, on_outage=_LOCKOUT_OUTAGE)


def _resolve_login_user(identifier: str) -> User | None:
    """Resolve a submitted login identifier to the account it would authenticate against.

    Used so failed-login tracking can be keyed by stable account id rather than by the raw submitted
    string.

    Args:
        identifier: The raw "username" field value as submitted.

    Returns:
        The matching User (active or not), or None if nothing resolves.
    """
    from urbanlens.dashboard.services.auth.identity import find_user_by_identifier

    return find_user_by_identifier(identifier, active_only=False)


def _lockout_key_for_user(user: User) -> str:
    """Return the stable lockout-key fragment for a resolved account."""
    return f"uid:{user.pk}"


def _raw_lockout_key(identifier: str) -> str:
    """Return a normalized fallback lockout-key fragment for an unresolved identifier.

    Collapses every spelling the login form would accept for one account, so probing variants of an
    identifier that matches no account is rate-limited under one shared key rather than each variant
    getting a fresh counter - it just isn't tied to a real account id.
    """
    from urbanlens.dashboard.services.auth.identity import canonical_identifier

    return f"raw:{canonical_identifier(identifier)}"


def _lockout_key_for_identifier(identifier: str) -> str:
    """Resolve a raw submitted login identifier to its lockout-counter key.

    Rotating through equivalent-but-textually-distinct identifiers for the same account (Gmail dot/plus
    variants, a verified secondary email) all collapse onto the same counter instead of each getting its
    own untripped one.

    Args:
        identifier: The raw "username" field value as submitted.

    Returns:
        A key fragment safe to interpolate into a cache key.
    """
    user = _resolve_login_user(identifier)
    return _lockout_key_for_user(user) if user is not None else _raw_lockout_key(identifier)


def _record_failed_attempt(key: str) -> int:
    """Increment the failure counter; apply lockout when the limit is reached.

    Args:
        key: The resolved lockout key (see ``_lockout_key_for_identifier``) for the identifier that just
        failed to authenticate.

    Returns:
        The updated failure count (after incrementing).
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    settings = SiteSettings.get_current()
    max_attempts = settings.login_max_attempts
    lockout_seconds = settings.login_lockout_minutes * 60

    if max_attempts <= 0:
        # Rate limiting disabled.
        return 0

    attempts_key = _attempts_key(key)
    attempts = _bump_counter(attempts_key, lockout_seconds)

    if attempts >= max_attempts:
        _set_lockout(_lockout_key(key), lockout_seconds)
        counters.clear(attempts_key)
        logger.warning("Login locked out for key %r after %d failed attempts", key, attempts)

    return attempts


def _clear_login_attempts(key: str) -> None:
    """Remove failure tracking after a successful login.

    Args:
        key: The resolved lockout key (see ``_lockout_key_for_user``) for the account that just
        authenticated successfully.
    """
    counters.clear(_attempts_key(key))
    counters.clear(_lockout_key(key))


def _lockout_error_message(minutes: int) -> str:
    """The error shown when a login attempt is refused at the lockout gate.

    A single source keeps the identifier-lockout and per-IP-throttle rejections byte-identical, so a
    refused attempt reveals neither which dimension (account or address) tripped nor whether the
    identifier exists.

    Args:
        minutes: The configured ``SiteSettings.login_lockout_minutes``.

    Returns:
        The user-facing error string.
    """
    return f"Too many failed login attempts. Please try again in {minutes} minute{'s' if minutes != 1 else ''}."


# -- Per-IP login failure throttle -------------------------------------------


def _login_ip_attempts_key(ip: str) -> str:
    """Cache key for the failed-login counter for a client IP."""
    return f"login_ip_attempts:{ip}"


def _is_ip_locked_out(request: HttpRequest) -> bool:
    """Return True if the requesting IP has exhausted its failed-login budget.

    Complements the per-identifier lockout: that one stops repeated attempts on a single account but
    lets one address spray attempts across many identifiers (and doubles as a targeted DoS, since anyone
    can trip it for a victim's identifier at no cost to themselves).

    Args:
        request: The incoming login request.

    Returns:
        True when the IP's failure count has reached ``SiteSettings.login_ip_max_attempts`` within the
        current window; always False when that...
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    max_attempts = SiteSettings.get_current().login_ip_max_attempts
    if max_attempts <= 0:
        return False
    return counters.peek(_login_ip_attempts_key(client_ip(request)), on_outage=_LOCKOUT_OUTAGE) >= max_attempts


def _record_login_ip_failure(request: HttpRequest) -> int:
    """Increment the requesting IP's failed-login counter.

    Failures only - a successful login never touches the counter, so the throttle simply expires
    ``login_lockout_minutes`` after the last recorded failure.

    Args:
        request: The login request that just failed.

    Returns:
        The updated failure count (0 when the throttle is disabled).
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    settings = SiteSettings.get_current()
    max_attempts = settings.login_ip_max_attempts
    if max_attempts <= 0:
        return 0

    key = _login_ip_attempts_key(client_ip(request))
    attempts = _bump_counter(key, settings.login_lockout_minutes * 60)

    if attempts >= max_attempts:
        logger.warning("Login throttled for IP %r after %d failed attempts", client_ip(request), attempts)

    return attempts


# -- Two-factor code rate limiting ------------------------------------------


def _two_factor_attempts_key(user_id: int) -> str:
    """Cache key for the failed TOTP/backup-code counter for a given user."""
    return f"2fa_code_attempts:{user_id}"


def _two_factor_lockout_key(user_id: int) -> str:
    """Cache key for the TOTP/backup-code lockout flag for a given user."""
    return f"2fa_code_lockout:{user_id}"


def _is_two_factor_locked_out(user_id: int) -> bool:
    """Return True if ``user_id`` is currently locked out of the code fallback."""
    return counters.peek(_two_factor_lockout_key(user_id), on_outage=_LOCKOUT_OUTAGE) > 0


def _record_two_factor_failure(user_id: int) -> int:
    """Increment the 2FA code failure counter; lock out once the limit is reached.

    Reuses ``SiteSettings.login_max_attempts``/``login_lockout_minutes`` so a password-verified attacker
    can't brute-force the TOTP/backup-code fallback within a single session.

    Args:
        user_id: Primary key of the user mid-2FA-challenge.

    Returns:
        The updated failure count (after incrementing).
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    settings = SiteSettings.get_current()
    max_attempts = settings.login_max_attempts
    lockout_seconds = settings.login_lockout_minutes * 60

    if max_attempts <= 0:
        return 0

    key = _two_factor_attempts_key(user_id)
    attempts = _bump_counter(key, lockout_seconds)

    if attempts >= max_attempts:
        _set_lockout(_two_factor_lockout_key(user_id), lockout_seconds)
        counters.clear(key)
        logger.warning("2FA code entry locked out for user id %r after %d failed attempts", user_id, attempts)

    return attempts


def _clear_two_factor_attempts(user_id: int) -> None:
    """Remove 2FA code failure tracking after a successful verification.

    Args:
        user_id: Primary key of the user who just verified successfully.
    """
    counters.clear(_two_factor_attempts_key(user_id))
    counters.clear(_two_factor_lockout_key(user_id))


# -- Registration form -----------------------------------------------------


class RegistrationForm(UserCreationForm):
    """Extends UserCreationForm to require an email address."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "you@example.com", "autocomplete": "email"}),
    )
    # Declared without the model field's validators, so every refusal is USERNAME_UNAVAILABLE.
    username = UsernameField(help_text=USERNAME_RULES)

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs["placeholder"] = "Choose a username"
        self.fields["username"].widget.attrs["autocomplete"] = "username"
        self.fields["password1"].widget.attrs["placeholder"] = "Create a password"
        self.fields["password1"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password2"].widget.attrs["placeholder"] = "Confirm your password"
        self.fields["password2"].widget.attrs["autocomplete"] = "new-password"

    def clean_email(self) -> str:
        """Lowercase the address. Whether it is taken is never a form error; see ``SignupView.form_valid``."""
        return self.cleaned_data["email"].strip().lower()

    def clean_username(self) -> str:
        """Refuse a malformed, reserved, taken or confusable username with one message that does not say which."""
        username = self.cleaned_data["username"]
        if not username_is_available(username):
            raise ValidationError(USERNAME_UNAVAILABLE, code="unavailable")
        return username

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_active = False  # Activated only after email verification
        if commit:
            user.save()
        return user


# -- Sign-up view ----------------------------------------------------------


class SignupView(generic.CreateView):
    """Create a new user account and send a verification email."""

    form_class = RegistrationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        from urbanlens.dashboard.models.site_settings import SiteSettings

        settings = SiteSettings.get_current()
        if settings.signup_restricted:
            invite_token = request.GET.get("invite") or request.POST.get("invite")
            if _invitation_page(invite_token) is None:
                return render(request, "registration/signup_restricted.html", status=403)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: RegistrationForm) -> HttpResponse:
        from django.contrib.auth.hashers import make_password

        from urbanlens.dashboard.services.auth.email_claims import defer
        from urbanlens.dashboard.tasks import process_signup

        email = form.cleaned_data["email"]
        invite_token = _coerce_invite_token(self.request.GET.get("invite") or self.request.POST.get("invite"))
        defer(
            process_signup,
            form.cleaned_data["username"],
            email,
            make_password(form.cleaned_data["password1"]),
            self.request.POST.get("e2ee_auth_salt", ""),
            str(invite_token) if invite_token else None,
        )
        self.request.session["pending_verification_email"] = email
        # A fast path for the invitation; the verification record carries it across browsers.
        if invite_token:
            self.request.session["pending_invite_token"] = str(invite_token)
        return redirect("verify_email_sent")


# -- Email verification views ----------------------------------------------


class VerifyEmailSentView(View):
    """Renders the 'check your email' confirmation page."""

    def get(self, request: HttpRequest) -> HttpResponse:
        email = request.session.pop("pending_verification_email", None)
        return render(request, "registration/verify_email_sent.html", {"email": email})


class VerifyEmailView(View):
    """Handles the click-through from the verification email link."""

    def get(self, request: HttpRequest, token) -> HttpResponse:
        verification = EmailVerification.objects.filter(token=token).select_related("user").first()

        if not verification:
            return render(
                request,
                "registration/verify_email_confirm.html",
                {
                    "valid": False,
                    "expired": False,
                },
            )

        if not verification.is_valid():
            return render(
                request,
                "registration/verify_email_confirm.html",
                {
                    "valid": False,
                    "expired": True,
                    "email": verification.user.email,
                },
            )

        verification.mark_verified()
        user = verification.user
        user.is_active = True
        user.save(update_fields=["is_active"])
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.auth.email_normalization import normalize_email

        Profile.objects.filter(user=user).update(verified_primary_email=normalize_email(user.email or ""))

        session_invite_token = request.session.pop("pending_invite_token", None)
        invite_token = session_invite_token or verification.pending_invite_token
        _process_pending_invitations(user)

        from urbanlens.dashboard.services.trips.trip_invitations import bind_invitations_to_account
        from urbanlens.dashboard.services.visits.visit_invites import process_pending_visit_invites

        process_pending_visit_invites(user)
        bind_invitations_to_account(user)

        invitation_page = _invitation_page(invite_token)
        next_url = f"{reverse('login')}?next={invitation_page}" if invitation_page else reverse("login")
        return render(request, "registration/verify_email_confirm.html", {"valid": True, "sign_in_url": next_url, "has_invitation": bool(invitation_page)})


class ResendVerificationView(View):
    """POST: resend a verification email by email address."""

    def get(self, request: HttpRequest) -> HttpResponse:
        email = request.GET.get("email", "")
        return render(request, "registration/resend_verification.html", {"email": email})

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.dashboard.services.auth.email_claims import defer
        from urbanlens.dashboard.tasks import resend_signup_verification

        email = request.POST.get("email", "").strip()
        if email:
            defer(resend_signup_verification, email)
            request.session["pending_verification_email"] = email
        return redirect("verify_email_sent")


# -- Password reset (E2EE-aware) --------------------------------------------


def sso_provider_hint(user: User) -> str:
    """Name the social-auth provider a passwordless account signed up through.

    Shared by the set-password prompt and the SSO-aware password reset form so the two surfaces never
    drift on how a provider is named.

    Args:
        user: The user to inspect.

    Returns:
        A display name like ``"Google"``/``"Discord"``, or the generic ``"a social account"`` fallback
        if no provider row is found.
    """
    provider = user.social_auth.values_list("provider", flat=True).first() if hasattr(user, "social_auth") else None
    return {"google-oauth2": "Google", "discord": "Discord"}.get(provider or "", "a social account")


class ResetPasswordWithApiKeyChoiceForm(SetPasswordForm):
    """The reset form, plus the offer to revoke API keys along with the password.

    Asked here rather than after the reset because ``post_reset_login`` is False: this POST is the only
    moment in the flow where the account is identified.
    A prompt on the next page would have no principal to act as.
    """

    revoke_api_keys = forms.BooleanField(
        required=False,
        label="Also revoke my API keys",
        help_text="Choose this if you think somebody else had access to your account.",
    )


class SsoAwarePasswordResetForm(PasswordResetForm):
    """PasswordResetForm that tells SSO-only accounts how to sign in (UL-257).

    Django's stock ``get_users()`` silently drops any account with ``has_usable_password() == False`` -
    correct for building a raw-password reset link, but the view shows the same generic "check your
    email" success page regardless, so an SSO-only user who requests a reset is told it worked and then
    never receives anything, with no hint that their account has no password to reset in the first
    place.
    This keeps the anti-enumeration property (the requester-facing response never reveals which branch
    fired, or whether the address matched at all) by still matching SSO-only accounts in
    ``get_users()``, then routing them to a different email in ``send_mail()`` that names their sign-in
    provider instead of a reset link.
    """

    def get_users(self, email: str):
        """Include SSO-only accounts alongside password-auth accounts, matched by any spelling of any of their addresses.

        The mail still goes to the account's primary address, never to the one typed.

        Args:
            email: The submitted email address.

        Returns:
            A generator of the active user matching ``email``, regardless of whether it has a usable
            password.
        """
        from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email

        user = find_user_by_email(email)
        return (u for u in ([user] if user is not None and user.email else []))

    def send_mail(
        self,
        subject_template_name: str,
        email_template_name: str,
        context: dict,
        from_email: str | None,
        to_email: str,
        html_email_template_name: str | None = None,
    ) -> None:
        """Route SSO-only accounts to the sign-in-hint email instead of a reset link.

        Args:
            subject_template_name: Password-auth path subject template.
            email_template_name: Password-auth path plain-text template.
            context: Rendering context built by ``save()`` (includes ``user``).
            from_email: Envelope from-address.
            to_email: Recipient address.
            html_email_template_name: Password-auth path HTML template.
        """
        user = context["user"]
        if not user.has_usable_password():
            super().send_mail(
                "registration/password_reset_sso_notice_subject.txt",
                "registration/password_reset_sso_notice_email.txt",
                {**context, "provider_hint": sso_provider_hint(user)},
                from_email,
                to_email,
                html_email_template_name="registration/password_reset_sso_notice_email.html",
            )
            return
        super().send_mail(subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=html_email_template_name)


class DeferredPasswordResetView(auth_views.PasswordResetView):
    """Password reset whose lookup and mail run after the response, so it costs the same for any address (P147)."""

    form_class = SsoAwarePasswordResetForm

    def form_valid(self, form: PasswordResetForm) -> HttpResponse:
        from urbanlens.dashboard.services.auth.email_claims import defer
        from urbanlens.dashboard.tasks import send_password_reset

        defer(send_password_reset, form.cleaned_data["email"])
        return HttpResponseRedirect(self.get_success_url())


class E2EEPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """PasswordResetConfirmView that keeps derived-auth accounts consistent.

    An email-link reset never sees the old password, which has two E2EE consequences this view handles:

    - Derived-auth accounts (``AccountKdf`` exists) get their new credential derived client-side; the
      fresh salt arrives as ``e2ee_auth_salt`` and replaces the sto...
    - Any password-wrapped private-key copy is now undecryptable (the old password is gone), so it is
      flagged stale. The next login from a device that still holds ...
    """

    form_class = ResetPasswordWithApiKeyChoiceForm

    def get_context_data(self, **kwargs) -> dict:
        """Add ``e2ee_mode`` and the API-key count the template's prompt needs.

        Args:
            **kwargs: Base context kwargs.

        Returns:
            The template context with ``e2ee_mode`` (``derived``/``legacy``) and ``active_api_key_count``.
        """
        from urbanlens.dashboard.models.account import AccountKdf
        from urbanlens.dashboard.services.auth.api_keys import active_api_key_count

        context = super().get_context_data(**kwargs)
        user = getattr(self, "user", None)
        context["e2ee_mode"] = "derived" if user is not None and AccountKdf.objects.for_user(user).exists() else "legacy"
        # Gated on validlink, not on `user`. The count only, never the key names: those are user-authored text
        # on a page that today reveals nothing about the account, and the settings page already lists them.
        context["active_api_key_count"] = active_api_key_count(user) if self.validlink and user is not None else 0
        return context

    def form_valid(self, form) -> HttpResponse:
        """Persist the new password, then reconcile the account's E2EE state.

        Args:
            form: The valid SetPasswordForm.

        Returns:
            The parent redirect response.
        """
        from urbanlens.dashboard.models.account import AccountKdf
        from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
        from urbanlens.dashboard.services.security.e2ee import MAX_SALT_LENGTH, valid_blob

        response = super().form_valid(form)
        user = form.user
        auth_salt = self.request.POST.get("e2ee_auth_salt", "")
        if valid_blob(auth_salt, MAX_SALT_LENGTH):
            AccountKdf.objects.set_auth_salt(user, auth_salt)
        else:
            AccountKdf.objects.for_user(user).delete()
        MessagingKeyBundle.objects.filter(profile__user=user).exclude(password_wrapped_secret="").update(password_wrap_stale=True)  # nosec B106 - "" is a field-emptiness filter, not a credential

        if form.cleaned_data.get("revoke_api_keys"):
            from urbanlens.dashboard.services.auth.api_keys import revoke_all_api_keys

            revoked = revoke_all_api_keys(user)
            # auth_base.html renders messages, and password_reset_complete extends it - so this is the one
            # surface that can confirm it, the account having no session to land in.
            messages.success(self.request, f"Revoked {revoked} API key{'' if revoked == 1 else 's'}. Any app using one will need a new key.")
        return response


# -- Custom login view -----------------------------------------------------


class CustomLoginView(LoginView):
    """LoginView extended with rate limiting and inactive-account detection.

    Rate limiting has two independent dimensions, both stored in Django's cache (no extra DB table
    needed) so they reset automatically when the cache is cleared or expires:

    - Per identifier: after ``SiteSettings.login_max_attempts`` consecutive failures the account is
      locked for ``SiteSettings.login_lockout_minutes`` minutes.
    - Per client IP: after ``SiteSettings.login_ip_max_attempts`` failures across *any* identifiers,
      further attempts from that address are refused for the same wi...
    """

    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Add ``is_first_run`` so the template can drop "Welcome back" on a fresh install.

        ``User.objects.exists()`` would already be True by the time this page is reached (registration
        creates the account *before* the user logs in), so this uses ``bootstrap_admin_onboarding_complete``
        instead - it defaults False for a genuinely empty site and stays False through the whole
        registration -> login -> setup-wizard journey, only flipping True once the bootstrap admin finishes
        onboarding (see ``services.admin.site_admin``), which is the actual window this copy should stay off
        for.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        context = super().get_context_data(**kwargs)
        context["is_first_run"] = not SiteSettings.get_current().bootstrap_admin_onboarding_complete
        return context

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "").strip()
        identifier_locked = bool(username) and _is_locked_out(_lockout_key_for_identifier(username))
        if identifier_locked or _is_ip_locked_out(request):
            from urbanlens.dashboard.models.site_settings import SiteSettings

            minutes = SiteSettings.get_current().login_lockout_minutes
            form = self.get_form()
            form.errors["__all__"] = form.error_class([_lockout_error_message(minutes)])
            # Deliberately not self.form_invalid: that override is the failure accounting path, and no
            # credential was checked here.
            return super().form_invalid(form)
        return super().post(request, *args, **kwargs)

    def get_success_url(self) -> str:
        redirect_to = self.get_redirect_url()
        if redirect_to:
            return redirect_to
        return reverse("post_login")

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        user = form.get_user()
        _clear_login_attempts(_lockout_key_for_user(user))

        from urbanlens.dashboard.services.auth.two_factor import has_second_factor

        if has_second_factor(user):
            # Password verified, but this account has a passkey and/or TOTP device
            # registered - hold off on auth_login() until the 2FA challenge succeeds.
            self.request.session[_WEBAUTHN_PENDING_USER_KEY] = user.pk
            self.request.session[_WEBAUTHN_PENDING_REDIRECT_KEY] = self.get_success_url()
            return redirect("login.2fa")
        return super().form_valid(form)

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        # Count every failure against the requesting IP too, whatever the identifier (or lack of one).
        if not _is_ip_locked_out(self.request):
            _record_login_ip_failure(self.request)

        username = form.data.get("username", "").strip()
        if username:
            # Keyed by account id, so every spelling of one account shares a counter.
            user = _resolve_login_user(username)
            lockout_key = _lockout_key_for_user(user) if user is not None else _raw_lockout_key(username)

            # Track failure and check for lockout (only when not already locked).
            if not _is_locked_out(lockout_key):
                _record_failed_attempt(lockout_key)
                if _is_locked_out(lockout_key):
                    from urbanlens.dashboard.models.site_settings import SiteSettings

                    minutes = SiteSettings.get_current().login_lockout_minutes
                    form.errors["__all__"] = form.error_class(
                        [f"Too many failed login attempts. Your account has been locked for {minutes} minute{'s' if minutes != 1 else ''}."],
                    )
                    return super().form_invalid(form)

        return super().form_invalid(form)


def _pending_2fa_user(request: HttpRequest) -> User | None:
    """Return the user mid-passkey-challenge in this session, or None.

    Args:
        request: The incoming request.

    Returns:
        The active User awaiting a passkey assertion, or None if there isn't one (either nothing
        pending, or the stashed id no longer resolves...
    """
    user_id = request.session.get(_WEBAUTHN_PENDING_USER_KEY)
    if not user_id:
        return None
    return User.objects.filter(pk=user_id, is_active=True).first()


def _two_factor_challenge_context(user: User, **extra: object) -> dict:
    """Shared template context for login_2fa.html: which options this account has."""
    from urbanlens.dashboard.services.auth.two_factor import has_totp, remaining_backup_code_count
    from urbanlens.dashboard.services.auth.webauthn import has_passkeys

    return {
        "username": user.username,
        "has_passkey": has_passkeys(user),
        "has_code_factor": has_totp(user) or remaining_backup_code_count(user) > 0,
        **extra,
    }


def _complete_two_factor_login(request: HttpRequest, user: User) -> str:
    """Finish a 2FA-gated login: establish the session and return the post-login redirect target."""
    redirect_to = request.session.pop(_WEBAUTHN_PENDING_REDIRECT_KEY, None) or reverse("post_login")
    request.session.pop(_WEBAUTHN_PENDING_USER_KEY, None)
    auth_login(request, user, backend="urbanlens.dashboard.services.auth.auth_backend.EmailOrUsernameModelBackend")
    return redirect_to


class LoginTwoFactorView(View):
    """Renders the 2FA challenge page reached after a password login.

    Only reachable via ``CustomLoginView.form_valid()`` stashing a pending user id in the session -
    visiting directly without that redirects to login.
    Offers a passkey prompt, a TOTP/backup-code form, or both, depending on what the account has
    configured.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("post_login")
        user = _pending_2fa_user(request)
        if user is None:
            return redirect("login")
        # A passkey-only account (no TOTP/backup codes) never renders this page's one {% csrf_token %} tag
        # (inside the code-fallback form), so Django would otherwise only set the csrftoken cookie here if an
        # earlier page in the session happened to. runLogin()'s options/verify fetches need that cookie
        # unconditionally - forcing it explicitly guarantees it regardless of how the user reached this page
        get_token(request)
        return render(request, "registration/login_2fa.html", _two_factor_challenge_context(user))


class LoginTwoFactorOptionsView(View):
    """POST: return WebAuthn authentication options for the pending user's passkeys."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return JsonResponse({"error": "No sign-in in progress. Please log in again."}, status=400)

        from urbanlens.dashboard.services.auth.webauthn import NoLoginPasskeysError, WebAuthnError, build_authentication_options

        try:
            options_json = build_authentication_options(request, user)
        except NoLoginPasskeysError as exc:
            logger.info("2fa passkey options rejected: %s", exc)
            return JsonResponse({"error": "This account has no passkeys registered."}, status=400)
        except WebAuthnError as exc:
            logger.info("2fa passkey options rejected: %s", exc)
            return JsonResponse({"error": "Passkey sign-in could not be started."}, status=400)
        return HttpResponse(options_json, content_type="application/json")


class LoginTwoFactorVerifyView(View):
    """POST: verify the browser's passkey assertion and complete login."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return JsonResponse({"error": "No sign-in in progress. Please log in again."}, status=400)

        from urbanlens.dashboard.services.auth.webauthn import (
            AuthenticationNotPendingError,
            AuthenticationVerificationError,
            CredentialNotRegisteredError,
            MalformedCredentialResponseError,
            WebAuthnError,
            verify_authentication,
        )

        try:
            verify_authentication(request, user, request.body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            logger.warning("Passkey verification body was not valid UTF-8: %s", exc, exc_info=True)
            return JsonResponse({"error": "Invalid passkey response."}, status=400)
        except AuthenticationNotPendingError as exc:
            logger.info("2fa passkey verification rejected: %s", exc)
            return JsonResponse({"error": "No passkey sign-in in progress. Please try again."}, status=400)
        except MalformedCredentialResponseError as exc:
            logger.info("2fa passkey verification rejected: %s", exc)
            return JsonResponse({"error": "Malformed passkey response."}, status=400)
        except CredentialNotRegisteredError as exc:
            logger.info("2fa passkey verification rejected: %s", exc)
            return JsonResponse({"error": "That passkey is not registered to this account."}, status=400)
        except AuthenticationVerificationError as exc:
            logger.info("2fa passkey verification rejected: %s", exc)
            return JsonResponse({"error": "That passkey could not be verified."}, status=400)
        except WebAuthnError as exc:
            logger.info("2fa passkey verification rejected: %s", exc)
            return JsonResponse({"error": "That passkey could not be verified."}, status=400)

        redirect_to = _complete_two_factor_login(request, user)
        return JsonResponse({"ok": True, "redirect": redirect_to})


class LoginTwoFactorCodeView(View):
    """POST: verify a TOTP or backup code - the non-JS fallback to a passkey assertion."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return redirect("login")

        if _is_two_factor_locked_out(user.pk):
            context = _two_factor_challenge_context(user, code_error="Too many incorrect attempts. Please wait before trying again.")
            return render(request, "registration/login_2fa.html", context, status=429)

        from urbanlens.dashboard.services.auth.two_factor import verify_login_code

        code = request.POST.get("code", "")
        if not verify_login_code(user, code):
            _record_two_factor_failure(user.pk)
            context = _two_factor_challenge_context(user, code_error="That code didn't work. Please try again.")
            return render(request, "registration/login_2fa.html", context, status=400)

        _clear_two_factor_attempts(user.pk)
        redirect_to = _complete_two_factor_login(request, user)
        return HttpResponseRedirect(redirect_to)


class LoginTwoFactorCancelView(View):
    """GET: abandon the pending passkey challenge and return to the login form."""

    def get(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(_WEBAUTHN_PENDING_USER_KEY, None)
        request.session.pop(_WEBAUTHN_PENDING_REDIRECT_KEY, None)
        return redirect("login")


#: How long "Not now" on the credential prompt stays quiet. Profile-persisted: the old per-session flag
#: re-nagged SSO users on every signin, which trained them to dismiss security prompts (see
#: docs/designs/e2ee-passkey-unlock.md).
CREDENTIAL_PROMPT_SNOOZE = timedelta(days=30)


def _needs_credential_prompt(profile) -> bool:
    """True when this account should see the add-a-passkey-or-password prompt.

    Args:
        profile: The signed-in user's profile.

    Returns:
        Whether to redirect through the prompt after login.
    """
    from urbanlens.dashboard.models.account import WebAuthnCredential
    from urbanlens.dashboard.models.e2ee import E2EEPasskeyWrap, MessagingKeyBundle

    user = profile.user
    if user.has_usable_password():
        return False
    if profile.credential_prompt_snoozed_until and profile.credential_prompt_snoozed_until > timezone.now():
        return False
    if not WebAuthnCredential.objects.filter(user=user).exists():
        return True
    bundle = MessagingKeyBundle.objects.filter(profile=profile).first()
    if bundle is None:
        return False
    return not E2EEPasskeyWrap.objects.usable_for_bundle(bundle).exists()


class SetPasswordPromptView(LoginRequiredMixin, View):
    """GET /accounts/set-password/ - offer a passwordless account a passkey or a password.

    "Not now" snoozes for ``CREDENTIAL_PROMPT_SNOOZE`` rather than one session.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.dashboard.models.profile.model import Profile

        # LoginRequiredMixin guarantees an authenticated request; going through
        # the profile keeps the User type concrete for the checks below.
        profile, _ = Profile.objects.get_or_create(user=request.user)
        user = profile.user
        if user.has_usable_password():
            return redirect("post_login")
        return render(
            request,
            "registration/set_password.html",
            {"self_slug": profile.ensure_slug(), "provider_hint": sso_provider_hint(user)},
        )


class SetPasswordSkipView(View):
    """POST /accounts/set-password/skip/ - snooze the prompt for a month.

    POST rather than GET because the snooze outlives the session: as a GET it was reachable by
    cross-site top-level navigation, which carries a SameSite=Lax session cookie, so any page could
    silence a security prompt for a month on the visitor's behalf.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.dashboard.models.profile.model import Profile

        if request.user.is_authenticated:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            profile.credential_prompt_snoozed_until = timezone.now() + CREDENTIAL_PROMPT_SNOOZE
            profile.save(update_fields=["credential_prompt_snoozed_until", "updated"])
        return redirect("post_login")


class PostLoginRedirectView(View):
    """Resolve the destination after password or OAuth login."""

    def get(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("login")

        redirect_to = request.GET.get(REDIRECT_FIELD_NAME, "")
        if redirect_to and url_has_allowed_host_and_scheme(
            redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return HttpResponseRedirect(redirect_to)

        if should_redirect_to_site_admin(request.user):
            return redirect("setup")

        from urbanlens.dashboard.models.profile.model import Profile

        try:
            profile = request.user.profile
        except Profile.DoesNotExist:
            profile, _ = Profile.objects.get_or_create(user=request.user)

        # Social-auth accounts have no usable password. Offer a passkey (or a password) so their encrypted
        # messages can unlock on a new device - once, with a month-long snooze, not per session.
        if _needs_credential_prompt(profile):
            return redirect("account.set_password")

        if not profile.welcome_onboarding_complete:
            return redirect("onboarding.welcome")

        if not profile.profile_setup_complete:
            return redirect("profile.edit")

        return redirect("map.view")


def _coerce_invite_token(invite_token: object) -> UUID | None:
    """Return a valid invite UUID or None for blank/malformed tokens."""
    if not invite_token:
        return None
    try:
        return UUID(str(invite_token))
    except (TypeError, ValueError, AttributeError):
        return None


# -- Invitation processing --------------------------------------------------


def _invitation_page(invite_token: object) -> str | None:
    """The response page of the open friend or trip invitation a signup link carried, if it is still open."""
    from urbanlens.dashboard.models.trips.invitation import TripInvitation
    from urbanlens.dashboard.services.social.friend_invitations import invitation_for_token

    token = _coerce_invite_token(invite_token)
    if token is None:
        return None
    if invitation_for_token(token) is not None:
        return reverse("friend.invitation", kwargs={"token": token})
    if TripInvitation.objects.filter(token=token, expires_at__gt=timezone.now()).exists():
        return reverse("trips.invitation", kwargs={"token": token})
    return None


def _process_pending_invitations(user: User) -> None:
    """After a new user's email is verified, show them the friend invitations sent to it. Answers none of them.

    Args:
        user: The newly-verified User.
    """
    from urbanlens.dashboard.services.social.friend_invitations import bind_to_new_account

    try:
        bind_to_new_account(user)
    except (AttributeError, DatabaseError):
        logger.exception("Error processing pending invitations for user %s", user.pk)


# -- Passphrase suggestions --------------------------------------------------


#: Per address: bounds bulk scraping of the wordlist. Applied in ``UrbanLens/urls.py``.
PASSPHRASE_SUGGEST_RATE = Rate(limit=30, window_seconds=60 * 10)
PASSPHRASE_SUGGEST_METHODS = frozenset({"GET"})

#: Per address: each check can reach HIBP. Applied in ``UrbanLens/urls.py``.
PASSWORD_POLICY_CHECK_RATE = Rate(limit=30, window_seconds=60 * 10)


@require_GET
def suggest_passphrases(request: HttpRequest) -> JsonResponse:
    """Return five strong passphrase suggestions for signup / password reset.

    Args:
        request: The incoming HTTP request.

    Returns:
        JSON with a ``passphrases`` list.
    """
    from urbanlens.dashboard.services.auth.passphrases import generate_passphrases

    return JsonResponse({"passphrases": generate_passphrases(5)})


#: Hard input cap - far above any legitimate passphrase, low enough that a
#: hostile client can't make the validator chain chew on megabytes.
_PASSWORD_CHECK_MAX_LENGTH = 1024


@require_POST
def validate_password_policy(request: HttpRequest) -> JsonResponse:
    """Run a candidate password through the configured ``AUTH_PASSWORD_VALIDATORS``.

    Exists for the E2EE signup / password-reset / password-change flows: the client derives the login
    credential from the raw password *before* submit, so the credential the server authenticates always
    "looks strong" and the configured validators (length 12, complexity, common-password, HIBP breach
    check) would otherwise never run against the real password at all.

    Args:
        request: JSON body with ``password`` plus optional ``username`` and ``email`` (fed to
        ``UserAttributeSimilarityValidator`` so "password resembles...

    Returns:
        JSON ``{valid, errors}`` (200), 400 on a malformed body, or 429 when rate-limited - callers
        treat any non-200 as "could not check" and...
    """
    from django.contrib.auth.password_validation import validate_password

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    password = body.get("password")
    if not isinstance(password, str) or not password:
        return JsonResponse({"error": "password is required."}, status=400)
    if len(password) > _PASSWORD_CHECK_MAX_LENGTH:
        return JsonResponse({"valid": False, "errors": ["This password is too long."]})

    # An unsaved User carrying the form's identity fields is exactly what
    # UserAttributeSimilarityValidator needs; nothing is persisted.
    raw_username = body.get("username")
    raw_email = body.get("email")
    username = raw_username if isinstance(raw_username, str) else ""
    email = raw_email if isinstance(raw_email, str) else ""
    candidate = User(username=username[:150], email=email[:254])
    try:
        validate_password(password, user=candidate)
    except ValidationError as exc:
        return JsonResponse({"valid": False, "errors": exc.messages})
    return JsonResponse({"valid": True, "errors": []})
