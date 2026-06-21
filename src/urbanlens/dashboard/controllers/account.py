"""Auth controllers: registration, email verification, login."""
from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING
from urllib.parse import quote

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.safestring import mark_safe
from django.views import View, generic

from urbanlens.dashboard.models.account import EmailVerification

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

logger = logging.getLogger(__name__)


# ── Registration form ─────────────────────────────────────────────────────

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
        """Reject duplicate email addresses (case-insensitive)."""
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists.")
        return email

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_active = False  # Activated only after email verification
        if commit:
            user.save()
        return user


# ── Sign-up view ──────────────────────────────────────────────────────────

class SignupView(generic.CreateView):
    """Create a new user account and send a verification email."""

    form_class = RegistrationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: RegistrationForm) -> HttpResponse:
        user = form.save()
        verification = EmailVerification.objects.create(user=user)
        self._send_verification_email(user, verification)
        # Store the email in session so the "check email" page can display it
        self.request.session["pending_verification_email"] = user.email
        # Store pending invite token (if the user arrived via an invitation link)
        invite_token = self.request.GET.get("invite") or self.request.POST.get("invite")
        if invite_token:
            self.request.session["pending_invite_token"] = invite_token
        return redirect("verify_email_sent")

    def _send_verification_email(self, user: User, verification: EmailVerification) -> None:
        verify_url = self.request.build_absolute_uri(
            reverse("verify_email", args=[str(verification.token)]),
        )
        context = {"user": user, "verify_url": verify_url}
        subject = "Verify your UrbanLens account"
        text_body = (
            f"Hi {user.username},\n\n"
            f"Please verify your email by visiting:\n{verify_url}\n\n"
            "This link expires in 48 hours.\n\n— UrbanLens"
        )
        html_body = render_to_string("registration/email/verify_email.html", context)

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=None,  # Uses DEFAULT_FROM_EMAIL
                to=[user.email],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()
            logger.info("Verification email sent to %s", user.email)
        except (smtplib.SMTPException, OSError):
            logger.exception("Failed to send verification email to %s", user.email)
            # Store the verify URL in session for debug display
            self.request.session["debug_verify_url"] = verify_url


# ── Email verification views ──────────────────────────────────────────────

class VerifyEmailSentView(View):
    """Renders the 'check your email' confirmation page."""

    def get(self, request: HttpRequest) -> HttpResponse:
        email = request.session.pop("pending_verification_email", None)
        debug_url = request.session.pop("debug_verify_url", None)
        return render(request, "registration/verify_email_sent.html", {
            "email": email,
            "debug_verify_url": debug_url,
        })


class VerifyEmailView(View):
    """Handles the click-through from the verification email link."""

    def get(self, request: HttpRequest, token) -> HttpResponse:
        verification = EmailVerification.objects.filter(token=token).select_related("user").first()

        if not verification:
            return render(request, "registration/verify_email_confirm.html", {
                "valid": False,
                "expired": False,
            })

        if not verification.is_valid():
            return render(request, "registration/verify_email_confirm.html", {
                "valid": False,
                "expired": True,
                "email": verification.user.email,
            })

        verification.mark_verified()
        user = verification.user
        user.is_active = True
        user.save(update_fields=["is_active"])

        # Auto-send friend request from any pending email invitations
        _process_pending_invitations(user)

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
            # Delete old token and create a fresh one
            EmailVerification.objects.filter(user=user).delete()
            verification = EmailVerification.objects.create(user=user)
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
    text_body = (
        f"Hi {user.username},\n\n"
        f"Please verify your email by visiting:\n{verify_url}\n\n"
        "This link expires in 48 hours.\n\n— UrbanLens"
    )
    html_body = render_to_string("registration/email/verify_email.html", context)
    try:
        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[user.email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send verification email to %s", user.email)
        request.session["debug_verify_url"] = verify_url


# ── Custom login view ─────────────────────────────────────────────────────

class CustomLoginView(LoginView):
    """LoginView extended to detect inactive-account failures and offer a resend link."""

    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        username = form.data.get("username", "").strip()
        if username:
            try:
                user = User.objects.get(username=username)
                if not user.is_active and hasattr(user, "email_verification"):
                    resend_url = reverse("resend_verification") + f"?email={quote(user.email)}"
                    form.errors["__all__"] = form.error_class([
                        mark_safe(  # noqa: S308 — URL and text are internal, no user input
                            "Your email address hasn't been verified yet. "
                            f'<a href="{resend_url}" class="auth-inline-link">Resend verification email</a>',
                        ),
                    ])
            except User.DoesNotExist:
                pass
        return super().form_invalid(form)


# ── Invitation processing ──────────────────────────────────────────────────

def _process_pending_invitations(user: User) -> None:
    """After a new user's email is verified, auto-create friend requests from any matching invitations.

    Args:
        user: The newly-verified User.
    """
    try:
        from django.utils import timezone

        from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
        from urbanlens.dashboard.models.friendship.model import Friendship

        pending = FriendInvitation.objects.filter(
            email__iexact=user.email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).select_related("inviter")

        profile = user.profile
        for invitation in pending:
            if invitation.inviter == profile:
                continue
            Friendship.request(from_profile=invitation.inviter, to_profile=profile.pk)
            invitation.mark_accepted()
    except Exception:
        logger.exception("Error processing pending invitations for %s", user.email)


# ── Legacy social_auth helper (kept for compatibility but not in URL routing) ──

def social_auth(request: HttpRequest, backend: str) -> HttpResponseRedirect:
    """Fallback used if the social pipeline doesn't handle redirect itself."""
    return redirect("map.view")
