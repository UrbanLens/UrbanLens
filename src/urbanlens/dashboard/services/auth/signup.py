"""Account mail whose work depends on whether an address is registered: signup, resend and password reset (P147).

The request only validates the form, then hands the rest to a task, so a registered address costs the submitter
exactly what a new one does. Which email goes out is the only difference, and only the address's owner sees it.
"""

from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse

from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.email_log.model import EmailType
from urbanlens.dashboard.services.auth.email_claims import absolute_url, address_holder, first_notice_this_hour, send_signup_notice
from urbanlens.dashboard.services.auth.username import username_is_taken
from urbanlens.dashboard.services.security.email_safety import email_rate_limit_error, record_email_sent, release_email_reservation, verification_recently_sent

if TYPE_CHECKING:
    import uuid

logger = logging.getLogger(__name__)


def is_abandoned_signup(user: User) -> bool:
    """Whether ``user`` is a signup whose verification link expired unused; it never proved the address, so it
    does not keep it from a new signup."""
    from urbanlens.dashboard.models.profile.model import Profile

    if user.is_active or Profile.objects.filter(user=user).exclude(verified_primary_email="").exists():
        return False
    verification = EmailVerification.objects.filter(user=user).first()
    return verification is not None and verification.verified_at is None and not verification.is_valid()


def store_signup_auth_salt(user: User, auth_salt: str) -> None:
    """Record a signup's client-side KDF salt, enrolling the account in derived auth."""
    from urbanlens.dashboard.models.account import AccountKdf
    from urbanlens.dashboard.services.security.e2ee import MAX_SALT_LENGTH, valid_blob

    if valid_blob(auth_salt, MAX_SALT_LENGTH):
        AccountKdf.objects.set_auth_salt(user, auth_salt)


def send_verification_email(user: User, verification: EmailVerification) -> None:
    """Email ``user`` the link that activates their account."""
    verify_url = absolute_url(reverse("verify_email", args=[str(verification.token)]))
    text_body = f"Hi {user.username},\n\nPlease verify your email by visiting:\n{verify_url}\n\nThis link expires in 48 hours.\n\n- UrbanLens"
    try:
        message = EmailMultiAlternatives(subject="Verify your UrbanLens account", body=text_body, from_email=None, to=[user.email])
        message.attach_alternative(render_to_string("registration/email/verify_email.html", {"user": user, "verify_url": verify_url}), "text/html")
        message.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send a verification email")
        if settings.DEBUG:
            logger.warning("Development only - verification link: %s", verify_url)


def complete_signup(username: str, email: str, password_hash: str, auth_salt: str, invite_token: uuid.UUID | None) -> None:
    """Create the account and send its verification link, or tell the address's owner someone tried to sign up.

    Args:
        username: The validated username.
        email: The address as typed, lowercased.
        password_hash: The already-hashed password.
        auth_salt: The client-side KDF salt, if the browser derived the credential.
        invite_token: The invitation the signup link carried, if any.
    """
    holder = address_holder(email)
    if holder is not None and is_abandoned_signup(holder):
        holder.delete()
        holder = None
    if holder is not None:
        send_signup_notice(email)
        return
    if username_is_taken(username):
        send_username_unavailable_notice(email, username)
        return
    try:
        with transaction.atomic():
            user = User.objects.create(username=username, email=email, password=password_hash, is_active=False)
            store_signup_auth_salt(user, auth_salt)
            verification = EmailVerification.objects.create(user=user, pending_invite_token=invite_token)
    except IntegrityError:
        send_username_unavailable_notice(email, username)
        return
    record_email_sent(user.profile, email, EmailType.EMAIL_VERIFICATION)
    send_verification_email(user, verification)


def send_username_unavailable_notice(email: str, username: str) -> None:
    """Tell a signup whose username stopped being available between the form and the task to sign up again."""
    if not first_notice_this_hour(email):
        return
    signup_url = absolute_url(reverse("signup"))
    text_body = f"Hi,\n\nYour UrbanLens signup didn't finish because the username {username} isn't available. No account was created. Please sign up again with a different username:\n{signup_url}\n\n- UrbanLens"
    try:
        EmailMultiAlternatives(subject="Your UrbanLens signup didn't finish", body=text_body, from_email=None, to=[email]).send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send a username-taken notice")


def send_password_reset(email: str) -> None:
    """Send a reset link, or an SSO sign-in hint, if ``email`` belongs to an active account."""
    from urllib.parse import urlsplit

    from urbanlens.dashboard.controllers.account import SsoAwarePasswordResetForm

    form = SsoAwarePasswordResetForm({"email": email})
    if not form.is_valid():
        return
    site = urlsplit(settings.SITE_URL)
    form.save(
        domain_override=site.netloc,
        use_https=site.scheme == "https",
        subject_template_name="registration/password_reset_subject.txt",
        email_template_name="registration/password_reset_email.txt",
        html_email_template_name="registration/password_reset_email.html",
    )


def charge_verification_mail(user: User) -> bool:
    """Charge one verification mail to ``user``'s address, or refuse it.

    Charged to the pending account's own ledger, so the cap follows the address whoever asks and from wherever:
    at most one per ``VERIFICATION_RESEND_COOLDOWN_SECONDS``, and never past the account's email budget.

    Args:
        user: The account the mail is for, at its primary address.

    Returns:
        Whether the mail may go out.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    profile = Profile.objects.filter(user=user).first()
    if profile is None or verification_recently_sent(profile, user.email):
        return False
    if email_rate_limit_error(profile) is not None:
        return False
    record_email_sent(profile, user.email, EmailType.EMAIL_VERIFICATION)
    release_email_reservation(profile)
    return True


def resend_verification(email: str) -> None:
    """Send a fresh verification link if ``email`` belongs to an account still awaiting one, within its cap."""
    user = address_holder(email)
    if user is None or user.is_active:
        return
    if not charge_verification_mail(user):
        return
    existing = EmailVerification.objects.filter(user=user).first()
    pending_invite_token = existing.pending_invite_token if existing else None
    with transaction.atomic():
        EmailVerification.objects.filter(user=user).delete()
        verification = EmailVerification.objects.create(user=user, pending_invite_token=pending_invite_token)
    send_verification_email(user, verification)
