"""Email normalization and cross-account lookup helpers.

Used everywhere an email address is matched against existing accounts (friend
invites, registration duplicate checks, profile contact settings, username-or-
email login) so that trivially distinct-looking addresses which route to the
same inbox are treated as the same account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalize_email(email: str) -> str:
    """Return a canonical form of ``email`` suitable for equality comparisons.

    Always lowercases and strips surrounding whitespace. For Gmail addresses
    (``gmail.com``/``googlemail.com``) also strips dots from the local part and
    anything from a ``+`` onward, since Gmail treats those as equivalent to the
    same mailbox (e.g. ``Jake.Smith+spam@gmail.com`` -> ``jakesmith@gmail.com``).

    Args:
        email: Raw email address as entered by a user.

    Returns:
        The normalized address. Never raises on malformed input - callers are
        expected to validate format separately (e.g. via ``validate_email``).
    """
    normalized = email.strip().lower()
    local, _, domain = normalized.rpartition("@")
    if not domain or domain not in _GMAIL_DOMAINS:
        return normalized

    local = local.split("+", 1)[0]
    local = local.replace(".", "")
    return f"{local}@{domain}"


def find_user_by_email(email: str, *, active_only: bool = True) -> User | None:
    """Look up a User whose primary or verified secondary email matches.

    Matching is done on the normalized form of ``email`` via the indexed
    ``Profile.primary_email_normalized`` cache and verified ``ProfileEmail``
    rows, so Gmail dot/plus variants and case differences all resolve to the
    same account without scanning every user.

    Args:
        email: Raw email address to look up.
        active_only: When True (the default - use this for friend matching,
            login, and duplicate checks), only accounts with ``is_active=True``
            match. Pass False only for UX helpers that need to find a
            not-yet-verified account (e.g. the login page's "resend
            verification" hint), never for anything that grants access.

    Returns:
        The matching User, or None if no account matches.
    """
    from urbanlens.dashboard.models.profile.email import ProfileEmail
    from urbanlens.dashboard.models.profile.model import Profile

    normalized = normalize_email(email)
    if not normalized:
        return None

    profiles = Profile.objects.filter(primary_email_normalized=normalized)
    if active_only:
        profiles = profiles.filter(user__is_active=True)
    profile = profiles.select_related("user").first()
    if profile:
        return profile.user

    secondary = ProfileEmail.objects.filter(is_verified=True, normalized_email=normalized)
    if active_only:
        secondary = secondary.filter(profile__user__is_active=True)
    match = secondary.select_related("profile__user").first()
    if match:
        return match.profile.user

    return None


def is_email_taken(email: str, *, exclude_user_id: int | None = None) -> bool:
    """Return True if ``email`` (normalized) is already the primary or a verified secondary email.

    Args:
        email: Candidate email address.
        exclude_user_id: Optional user primary key to ignore (for self-edits).

    Returns:
        True when the normalized address collides with another account.
    """
    from urbanlens.dashboard.models.profile.email import ProfileEmail
    from urbanlens.dashboard.models.profile.model import Profile

    normalized = normalize_email(email)
    if not normalized:
        return False

    primary = Profile.objects.filter(primary_email_normalized=normalized)
    if exclude_user_id is not None:
        primary = primary.exclude(user_id=exclude_user_id)
    if primary.exists():
        return True

    secondary = ProfileEmail.objects.filter(is_verified=True, normalized_email=normalized)
    if exclude_user_id is not None:
        secondary = secondary.exclude(profile__user_id=exclude_user_id)
    return secondary.exists()
