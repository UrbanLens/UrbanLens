"""Auth controllers: registration, email verification, login."""

from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import UUID

from django import forms
from django.contrib.auth import REDIRECT_FIELD_NAME, login as auth_login, views as auth_views
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View, generic
from django.views.decorators.http import require_GET

from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.services.site_admin import should_redirect_to_site_admin
from urbanlens.dashboard.services.two_factor import SESSION_WEBAUTHN_PENDING_REDIRECT as _WEBAUTHN_PENDING_REDIRECT_KEY, SESSION_WEBAUTHN_PENDING_USER as _WEBAUTHN_PENDING_USER_KEY
from urbanlens.dashboard.services.username import USERNAME_RE, username_is_taken

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_PASSPHRASE_RATE_KEY = "passphrase_suggest:{ip}"  # noqa: S105  # nosec B105 - cache key template, not a credential
_PASSPHRASE_RATE_LIMIT = 30  # suggestion batches per IP per window
_PASSPHRASE_RATE_WINDOW = 60 * 10  # 10 minutes


# -- Login rate limiting helpers ------------------------------------------------


def _attempts_key(username: str) -> str:
    """Cache key for the failed-attempt counter for a given username."""
    return f"login_attempts:{username.strip().lower()}"


def _lockout_key(username: str) -> str:
    """Cache key for the lockout flag for a given username."""
    return f"login_lockout:{username.strip().lower()}"


def _is_locked_out(username: str) -> bool:
    """Return True if ``username`` is currently locked out."""
    return bool(cache.get(_lockout_key(username)))


def _record_failed_attempt(username: str) -> int:
    """Increment the failure counter; apply lockout when the limit is reached.

    Args:
        username: The username that just failed to authenticate.

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

    key = _attempts_key(username)
    attempts: int = (cache.get(key) or 0) + 1
    cache.set(key, attempts, timeout=lockout_seconds)

    if attempts >= max_attempts:
        cache.set(_lockout_key(username), 1, timeout=lockout_seconds)
        cache.delete(key)
        logger.warning("Login locked out for username %r after %d failed attempts", username, attempts)

    return attempts


def _clear_login_attempts(username: str) -> None:
    """Remove failure tracking after a successful login.

    Args:
        username: The username that just authenticated successfully.
    """
    cache.delete(_attempts_key(username))
    cache.delete(_lockout_key(username))


# -- Registration form -----------------------------------------------------


class RegistrationForm(UserCreationForm):
    """Extends UserCreationForm to require an email address."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "you@example.com", "autocomplete": "email"}),
    )

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
        """Reject email addresses already in use (normalized comparison)."""
        from urbanlens.dashboard.services.email_normalization import is_email_taken

        email = self.cleaned_data["email"].strip().lower()
        if is_email_taken(email):
            raise ValidationError("An account with this email address already exists.")
        return email

    def clean_username(self) -> str:
        """Reject usernames that collide case- or confusably-insensitively."""
        username = super().clean_username()
        if not USERNAME_RE.match(username):
            raise ValidationError("3-30 characters: letters, numbers, and underscores only.")
        if username_is_taken(username):
            raise ValidationError("A user with that username already exists.")
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
            if not invite_token:
                return render(request, "registration/signup_restricted.html", status=403)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: RegistrationForm) -> HttpResponse:
        user = form.save()
        _store_signup_auth_salt(user, self.request.POST.get("e2ee_auth_salt", ""))
        invite_token = self.request.GET.get("invite") or self.request.POST.get("invite")
        verification = EmailVerification.objects.create(
            user=user,
            pending_invite_token=_coerce_invite_token(invite_token),
        )
        self._send_verification_email(user, verification)
        # Store the email in session so the "check email" page can display it
        self.request.session["pending_verification_email"] = user.email
        # Store pending invite token (if the user arrived via an invitation link)
        # in the session as a fast path and on the verification record so invite
        # acceptance survives opening the verification email in a different browser.
        if _coerce_invite_token(invite_token):
            self.request.session["pending_invite_token"] = invite_token
        return redirect("verify_email_sent")

    def _send_verification_email(self, user: User, verification: EmailVerification) -> None:
        verify_url = self.request.build_absolute_uri(
            reverse("verify_email", args=[str(verification.token)]),
        )
        context = {"user": user, "verify_url": verify_url}
        subject = "Verify your UrbanLens account"
        text_body = f"Hi {user.username},\n\nPlease verify your email by visiting:\n{verify_url}\n\nThis link expires in 48 hours.\n\n- UrbanLens"
        html_body = render_to_string("registration/email/verify_email.html", context)

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=None,  # Uses UL_EMAIL_FROM
                to=[user.email],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()
            logger.info("Verification email sent to %s", user.email)
        except (smtplib.SMTPException, OSError):
            logger.exception("Failed to send verification email to %s", user.email)
            # Store the verify URL in session for debug display
            self.request.session["debug_verify_url"] = verify_url


def _store_signup_auth_salt(user: User, auth_salt: str) -> None:
    """Record a signup's client-side KDF salt, enrolling the account in derived auth.

    When the signup form's JS derived the login credential in the browser, the
    salt it used arrives as ``e2ee_auth_salt`` - storing it is what makes the
    login page derive the same credential later. A signup without it (JS
    unavailable) simply stays a legacy raw-password account and upgrades
    transparently on first login.

    Args:
        user: The newly created user.
        auth_salt: The base64 salt from the signup POST, possibly blank.
    """
    from urbanlens.dashboard.models.account import AccountKdf
    from urbanlens.dashboard.services.e2ee import MAX_SALT_LENGTH, valid_blob

    if valid_blob(auth_salt, MAX_SALT_LENGTH):
        AccountKdf.objects.set_auth_salt(user, auth_salt)


# -- Email verification views ----------------------------------------------


class VerifyEmailSentView(View):
    """Renders the 'check your email' confirmation page."""

    def get(self, request: HttpRequest) -> HttpResponse:
        email = request.session.pop("pending_verification_email", None)
        debug_url = request.session.pop("debug_verify_url", None)
        return render(
            request,
            "registration/verify_email_sent.html",
            {
                "email": email,
                "debug_verify_url": debug_url,
            },
        )


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

        # Auto-send friend request from any pending email invitations
        session_invite_token = request.session.pop("pending_invite_token", None)
        invite_token = session_invite_token or verification.pending_invite_token
        _process_pending_invitations(user, invite_token=str(invite_token) if invite_token else None)

        # Deliver any friend requests + visit suggestions that were waiting on
        # this email address (visit participants tagged before the account existed).
        from urbanlens.dashboard.services.visit_invites import process_pending_visit_invites

        process_pending_visit_invites(user)

        return render(request, "registration/verify_email_confirm.html", {"valid": True})


class ResendVerificationView(View):
    """POST: resend a verification email by email address."""

    def get(self, request: HttpRequest) -> HttpResponse:
        email = request.GET.get("email", "")
        return render(request, "registration/resend_verification.html", {"email": email})

    def post(self, request: HttpRequest) -> HttpResponse:
        email = request.POST.get("email", "").strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=False).first()
        if user:
            # Delete old token and create a fresh one while preserving any
            # signup invite token captured before the verification resend.
            existing_verification = EmailVerification.objects.filter(user=user).first()
            pending_invite_token = existing_verification.pending_invite_token if existing_verification else None
            EmailVerification.objects.filter(user=user).delete()
            verification = EmailVerification.objects.create(
                user=user,
                pending_invite_token=pending_invite_token,
            )
            _send_verification_email(request, user, verification)
            request.session["pending_verification_email"] = user.email
        # Always redirect to "sent" page (don't reveal whether email exists)
        return redirect("verify_email_sent")


def _send_verification_email(request: HttpRequest, user: User, verification: EmailVerification) -> None:
    """Shared helper used by ResendVerificationView."""
    verify_url = request.build_absolute_uri(
        reverse("verify_email", args=[str(verification.token)]),
    )
    context = {"user": user, "verify_url": verify_url}
    subject = "Verify your UrbanLens account"
    text_body = f"Hi {user.username},\n\nPlease verify your email by visiting:\n{verify_url}\n\nThis link expires in 48 hours.\n\n- UrbanLens"
    html_body = render_to_string("registration/email/verify_email.html", context)
    try:
        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[user.email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send verification email to %s", user.email)
        request.session["debug_verify_url"] = verify_url


# -- Password reset (E2EE-aware) --------------------------------------------


class E2EEPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """PasswordResetConfirmView that keeps derived-auth accounts consistent.

    An email-link reset never sees the old password, which has two E2EE
    consequences this view handles:

    - Derived-auth accounts (``AccountKdf`` exists) get their new credential
      derived client-side; the fresh salt arrives as ``e2ee_auth_salt`` and
      replaces the stored one. If the field is missing (JS failed), the
      ``AccountKdf`` row is deleted so the account reverts to legacy
      raw-password auth instead of being locked out by a derivation mismatch -
      it re-upgrades transparently at the next login.
    - Any password-wrapped private-key copy is now undecryptable (the old
      password is gone), so it is flagged stale. The next login from a device
      that still holds the cached key silently re-wraps it; a cold device
      falls back to the recovery key.
    """

    def get_context_data(self, **kwargs) -> dict:
        """Add ``e2ee_mode`` so the template's JS knows whether to derive.

        Args:
            **kwargs: Base context kwargs.

        Returns:
            The template context with ``e2ee_mode`` (``derived``/``legacy``).
        """
        from urbanlens.dashboard.models.account import AccountKdf

        context = super().get_context_data(**kwargs)
        user = getattr(self, "user", None)
        context["e2ee_mode"] = "derived" if user is not None and AccountKdf.objects.for_user(user).exists() else "legacy"
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
        from urbanlens.dashboard.services.e2ee import MAX_SALT_LENGTH, valid_blob

        response = super().form_valid(form)
        user = form.user
        auth_salt = self.request.POST.get("e2ee_auth_salt", "")
        if valid_blob(auth_salt, MAX_SALT_LENGTH):
            AccountKdf.objects.set_auth_salt(user, auth_salt)
        else:
            AccountKdf.objects.for_user(user).delete()
        MessagingKeyBundle.objects.filter(profile__user=user).exclude(password_wrapped_secret="").update(password_wrap_stale=True)  # nosec B106 - "" is a field-emptiness filter, not a credential
        return response


# -- Custom login view -----------------------------------------------------


class CustomLoginView(LoginView):
    """LoginView extended with rate limiting and inactive-account detection.

    Rate limiting is based on the username: after ``SiteSettings.login_max_attempts``
    consecutive failures the account is locked for ``SiteSettings.login_lockout_minutes``
    minutes.  The lockout state is stored in Django's cache (no extra DB table needed)
    so it resets automatically when the cache is cleared or expires.

    Setting ``login_max_attempts`` to 0 in site admin disables rate limiting entirely.
    """

    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "").strip()
        if username and _is_locked_out(username):
            from urbanlens.dashboard.models.site_settings import SiteSettings

            minutes = SiteSettings.get_current().login_lockout_minutes
            form = self.get_form()
            form.errors["__all__"] = form.error_class(
                [f"Too many failed login attempts. Please try again in {minutes} minute{'s' if minutes != 1 else ''}."],
            )
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def get_success_url(self) -> str:
        redirect_to = self.get_redirect_url()
        if redirect_to:
            return redirect_to
        return reverse("post_login")

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        username = form.cleaned_data.get("username", "")
        _clear_login_attempts(username)
        user = form.get_user()

        from urbanlens.dashboard.services.two_factor import has_second_factor

        if has_second_factor(user):
            # Password verified, but this account has a passkey and/or TOTP device
            # registered - hold off on auth_login() until the 2FA challenge succeeds.
            self.request.session[_WEBAUTHN_PENDING_USER_KEY] = user.pk
            self.request.session[_WEBAUTHN_PENDING_REDIRECT_KEY] = self.get_success_url()
            return redirect("login.2fa")
        return super().form_valid(form)

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        username = form.data.get("username", "").strip()
        if username:
            # Track failure and check for lockout (only when not already locked).
            if not _is_locked_out(username):
                _record_failed_attempt(username)
                if _is_locked_out(username):
                    from urbanlens.dashboard.models.site_settings import SiteSettings

                    minutes = SiteSettings.get_current().login_lockout_minutes
                    form.errors["__all__"] = form.error_class(
                        [f"Too many failed login attempts. Your account has been locked for {minutes} minute{'s' if minutes != 1 else ''}."],
                    )
                    return super().form_invalid(form)

            # Check for unverified account (username or email login).
            user: User | None = None
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                from urbanlens.dashboard.services.email_normalization import find_user_by_email

                user = find_user_by_email(username, active_only=False) if "@" in username else None

            if user is not None:
                if not user.is_active and hasattr(user, "email_verification"):
                    resend_url = reverse("resend_verification") + f"?email={quote(user.email)}"
                    form.errors["__all__"] = form.error_class(
                        [
                            format_html(
                                'Your email address hasn\'t been verified yet. <a href="{}" class="auth-inline-link">Resend verification email</a>',
                                resend_url,
                            ),
                        ],
                    )
        return super().form_invalid(form)


def _pending_2fa_user(request: HttpRequest) -> User | None:
    """Return the user mid-passkey-challenge in this session, or None.

    Args:
        request: The incoming request.

    Returns:
        The active User awaiting a passkey assertion, or None if there isn't one
        (either nothing pending, or the stashed id no longer resolves to an active user).
    """
    user_id = request.session.get(_WEBAUTHN_PENDING_USER_KEY)
    if not user_id:
        return None
    return User.objects.filter(pk=user_id, is_active=True).first()


def _two_factor_challenge_context(user: User, **extra: object) -> dict:
    """Shared template context for login_2fa.html: which options this account has."""
    from urbanlens.dashboard.services.two_factor import has_totp, remaining_backup_code_count
    from urbanlens.dashboard.services.webauthn import has_passkeys

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
    auth_login(request, user, backend="urbanlens.dashboard.services.auth_backend.EmailOrUsernameModelBackend")
    return redirect_to


class LoginTwoFactorView(View):
    """Renders the 2FA challenge page reached after a password login.

    Only reachable via ``CustomLoginView.form_valid()`` stashing a pending user
    id in the session - visiting directly without that redirects to login.
    Offers a passkey prompt, a TOTP/backup-code form, or both, depending on
    what the account has configured.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("post_login")
        user = _pending_2fa_user(request)
        if user is None:
            return redirect("login")
        return render(request, "registration/login_2fa.html", _two_factor_challenge_context(user))


class LoginTwoFactorOptionsView(View):
    """POST: return WebAuthn authentication options for the pending user's passkeys."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return JsonResponse({"error": "No sign-in in progress. Please log in again."}, status=400)

        from urbanlens.dashboard.services.webauthn import WebAuthnError, build_authentication_options

        try:
            options_json = build_authentication_options(request, user)
        except WebAuthnError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return HttpResponse(options_json, content_type="application/json")


class LoginTwoFactorVerifyView(View):
    """POST: verify the browser's passkey assertion and complete login."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return JsonResponse({"error": "No sign-in in progress. Please log in again."}, status=400)

        from urbanlens.dashboard.services.webauthn import WebAuthnError, verify_authentication

        try:
            verify_authentication(request, user, request.body.decode("utf-8"))
        except (WebAuthnError, UnicodeDecodeError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        redirect_to = _complete_two_factor_login(request, user)
        return JsonResponse({"ok": True, "redirect": redirect_to})


class LoginTwoFactorCodeView(View):
    """POST: verify a TOTP or backup code - the non-JS fallback to a passkey assertion."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _pending_2fa_user(request)
        if user is None:
            return redirect("login")

        from urbanlens.dashboard.services.two_factor import verify_login_code

        code = request.POST.get("code", "")
        if not verify_login_code(user, code):
            context = _two_factor_challenge_context(user, code_error="That code didn't work. Please try again.")
            return render(request, "registration/login_2fa.html", context, status=400)

        redirect_to = _complete_two_factor_login(request, user)
        return HttpResponseRedirect(redirect_to)


class LoginTwoFactorCancelView(View):
    """GET: abandon the pending passkey challenge and return to the login form."""

    def get(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(_WEBAUTHN_PENDING_USER_KEY, None)
        request.session.pop(_WEBAUTHN_PENDING_REDIRECT_KEY, None)
        return redirect("login")


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


def _collect_pending_invitations(user: User, invite_token: str | None) -> list:
    """Return open invitations matching the user's email and/or signup invite token."""
    from django.utils import timezone

    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation

    pending_by_id: dict[int, FriendInvitation] = {}

    for invitation in FriendInvitation.objects.filter(
        email__iexact=user.email,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).select_related("inviter"):
        pending_by_id[invitation.pk] = invitation

    if invite_token:
        token_invitation = (
            FriendInvitation.objects.filter(
                token=invite_token,
                accepted_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .select_related("inviter")
            .first()
        )
        if token_invitation:
            pending_by_id[token_invitation.pk] = token_invitation

    return list(pending_by_id.values())


def _apply_pending_invitation(invitation, profile) -> None:
    """Create a friend request and notification for one pending invitation."""
    from urbanlens.dashboard.controllers.friendship import notify_friend_request
    from urbanlens.dashboard.models.friendship.model import Friendship

    if invitation.inviter == profile:
        return
    friendship = Friendship.request(from_profile=invitation.inviter, to_profile=profile.pk, message=invitation.message)
    if friendship:
        notify_friend_request(invitation.inviter, profile, invitation.message)
    from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant, grant_subscription

    for pending_grant in PendingSubscriptionGrant.objects.for_invitation(invitation):
        grant_subscription(profile.user, pending_grant.role, pending_grant.granted_by, pending_grant.duration_as_int())
    invitation.mark_accepted()


def _process_pending_invitations(user: User, invite_token: str | None = None) -> None:
    """After a new user's email is verified, auto-create friend requests from any matching invitations.

    Args:
        user: The newly-verified User.
        invite_token: Optional invitation token stored during signup from an invite link.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    try:
        profile, _ = Profile.objects.get_or_create(user=user)
        for invitation in _collect_pending_invitations(user, invite_token):
            _apply_pending_invitation(invitation, profile)
    except (AttributeError, DatabaseError):
        logger.exception("Error processing pending invitations for %s", user.email)


# -- Passphrase suggestions --------------------------------------------------


def _client_ip(request: HttpRequest) -> str:
    """Best-effort client IP for rate limiting passphrase suggestions.

    Args:
        request: The incoming HTTP request.

    Returns:
        A string suitable for use as a cache-key fragment.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.META.get("REMOTE_ADDR") or "unknown"


@require_GET
def suggest_passphrases(request: HttpRequest) -> JsonResponse:
    """Return five strong passphrase suggestions for signup / password reset.

    Rate-limited per client IP to deter bulk scraping of the wordlist.

    Args:
        request: The incoming HTTP request.

    Returns:
        JSON with a ``passphrases`` list, or 429 when the rate limit is hit.
    """
    from urbanlens.dashboard.services.passphrases import generate_passphrases

    key = _PASSPHRASE_RATE_KEY.format(ip=_client_ip(request))
    hits = int(cache.get(key) or 0)
    if hits >= _PASSPHRASE_RATE_LIMIT:
        return JsonResponse({"error": "Too many requests. Try again in a few minutes."}, status=429)
    cache.set(key, hits + 1, timeout=_PASSPHRASE_RATE_WINDOW)
    return JsonResponse({"passphrases": generate_passphrases(5)})
