"""Signing up with, or changing to, an email address without revealing whether another account holds it (P147).

Whatever the address, the person submitting it sees the same result. What differs is the email that goes to the
address: a new one gets a link to confirm it, one that already has an account gets a notice instead, so only
its owner learns anything.
"""

from __future__ import annotations

import hashlib
import logging
import smtplib
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse

from urbanlens.dashboard.models.email_log.model import EmailType
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email, is_email_taken, normalize_email
from urbanlens.dashboard.services.security.email_safety import email_rate_limit_error, record_email_sent, release_email_reservation

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

NOTICE_INTERVAL_SECONDS = 60 * 60


class EmailClaimError(ValueError):
    """The request cannot go ahead for a reason that depends only on what the requester typed or has done."""


def _send(to: str, subject: str, text_body: str, html_template: str, context: dict) -> None:
    try:
        message = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[to])
        message.attach_alternative(render_to_string(html_template, context), "text/html")
        message.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send %r", subject)


def _first_notice_this_hour(address: str) -> bool:
    """Whether a notice may go to ``address`` now; at most one an hour, so a form can't be used to spam it."""
    key = "email-claim-notice:" + hashlib.sha256(normalize_email(address).encode()).hexdigest()
    return cache.add(key, 1, NOTICE_INTERVAL_SECONDS)


def address_holder(email: str) -> User | None:
    """The account holding ``email`` as its primary (verified or not, active or not) or as a verified secondary."""
    return find_user_by_email(email, active_only=False)


def send_signup_notice(email: str, *, url_builder: Callable[[str], str]) -> None:
    """Tell the owner of a registered address that someone tried to sign up with it."""
    if not _first_notice_this_hour(email):
        return
    login_url = url_builder(reverse("login"))
    reset_url = url_builder(reverse("password_reset"))
    text_body = (
        "Hi,\n\nSomeone (possibly you) tried to create a new UrbanLens account with this email address, which "
        f"already has one.\n\nTo sign in to your existing account:\n{login_url}\n\n"
        f"If you've forgotten your password, you can reset it here:\n{reset_url}\n\n"
        "If this wasn't you, you can safely ignore this email; nothing has changed.\n\n- UrbanLens"
    )
    _send(email, "Sign in to your UrbanLens account", text_body, "registration/email/signup_notice.html", {"login_url": login_url, "reset_url": reset_url})


def send_address_in_use_notice(email: str) -> None:
    """Tell the owner of a registered address that another account tried to add it. There is nothing for them to do."""
    if not _first_notice_this_hour(email):
        return
    text_body = "Hi,\n\nSomeone tried to add this email address to a different UrbanLens account. It is already used by your account, so nothing has changed and it stays with you.\n\nThere's nothing you need to do.\n\n- UrbanLens"
    _send(email, "Your email address on UrbanLens", text_body, "dashboard/email/address_in_use.html", {})


def send_confirmation(claim: ProfileEmail, *, url_builder: Callable[[str], str]) -> None:
    """Send whatever is due to a claimed address: a confirmation link if it is free, otherwise the in-use notice."""
    if is_email_taken(claim.email, exclude_user_id=claim.profile.user_id):
        send_address_in_use_notice(claim.email)
        return
    verify_url = url_builder(reverse("profile.email.verify", args=[str(claim.verification_token)]))
    purpose = "make it the address you sign in with" if claim.promote_on_verify else "so it can be used to find your account and to log in"
    text_body = f"Hi {claim.profile.username},\n\nConfirm this email address {purpose}:\n{verify_url}\n\nIf you didn't request this, you can ignore this email.\n\n- UrbanLens"
    _send(claim.email, "Confirm your email address for UrbanLens", text_body, "dashboard/email/verify_profile_email.html", {"profile": claim.profile, "verify_url": verify_url, "make_primary": claim.promote_on_verify})


def claim_address(profile: Profile, raw: str, *, make_primary: bool, url_builder: Callable[[str], str]) -> ProfileEmail:
    """Record ``raw`` as pending for ``profile`` and email it; the same for an address another account holds.

    Args:
        profile: The account claiming the address.
        raw: The address as typed.
        make_primary: Whether it replaces the primary address once confirmed.
        url_builder: Builds an absolute URL from a site-relative path.

    Returns:
        The pending claim.

    Raises:
        EmailClaimError: The address is malformed, already this account's, or the email budget is spent.
    """
    email = (raw or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise EmailClaimError("Enter a valid email address.") from exc
    normalized = normalize_email(email)
    if normalized == normalize_email(profile.user.email or ""):
        raise EmailClaimError("That's already your email address.")

    own = profile.secondary_emails.filter(normalized_email=normalized).first()
    if own is not None and (own.is_verified or not make_primary):
        if make_primary:
            promote(own)
            return own
        raise EmailClaimError("You've already added that email address.")

    limit_error = email_rate_limit_error(profile)
    if limit_error:
        raise EmailClaimError(limit_error)
    with transaction.atomic():
        if make_primary:
            profile.secondary_emails.filter(promote_on_verify=True).exclude(pk=getattr(own, "pk", None)).update(promote_on_verify=False)
        if own is None:
            own = ProfileEmail.objects.create(profile=profile, email=email, promote_on_verify=make_primary)
        elif make_primary and not own.promote_on_verify:
            own.promote_on_verify = True
            own.save(update_fields=["promote_on_verify", "updated"])
        record_email_sent(profile, email, EmailType.EMAIL_VERIFICATION)
    release_email_reservation(profile)
    send_confirmation(own, url_builder=url_builder)
    return own


def promote(claim: ProfileEmail) -> None:
    """Make a verified claim the account's primary address, replacing the old one."""
    from urbanlens.dashboard.models.profile.model import Profile

    user = claim.profile.user
    user.email = claim.email
    user.save(update_fields=["email"])
    Profile.objects.filter(pk=claim.profile_id).update(verified_primary_email=claim.normalized_email)
    claim.delete()


def confirm(claim: ProfileEmail) -> str | None:
    """Verify a claim from its emailed link, and promote it if it was a primary change.

    Returns:
        An error for the mailbox's owner, or None on success.
    """
    if is_email_taken(claim.email, exclude_user_id=claim.profile.user_id):
        return "That email address is already used by another account."
    try:
        claim.mark_verified()
    except IntegrityError:
        return "That email address is already used by another account."
    if claim.promote_on_verify:
        promote(claim)
    return None
