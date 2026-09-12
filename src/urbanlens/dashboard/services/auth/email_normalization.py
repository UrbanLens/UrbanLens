"""Email normalization and cross-account lookup helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalize_email(email: str) -> str:
    """Return a canonical form of ``email`` suitable for equality comparisons.

    Args:
        email: Raw email address as entered by a user.

    Returns:
        The normalized address."""
    normalized = email.strip().lower()
    local, _, domain = normalized.rpartition("@")
    if not domain or domain not in _GMAIL_DOMAINS:
        return normalized

    local = local.split("+", 1)[0]
    local = local.replace(".", "")
    return f"{local}@{domain}"


def find_user_by_email(email: str, *, active_only: bool = True) -> User | None:
    """Look up a User whose primary or verified secondary email matches.

    Args:
        email: Raw email address to look up.
        active_only: When True (the default - use this for friend matching, login, and duplicate checks), only accounts with ``is_active=True`` match.

    Returns:
        The matching User, or None if no account matches."""
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

    secondary = ProfileEmail.objects.verified_for(normalized)
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

    secondary = ProfileEmail.objects.verified_for(normalized)
    if exclude_user_id is not None:
        secondary = secondary.exclude(profile__user_id=exclude_user_id)
    return secondary.exists()
