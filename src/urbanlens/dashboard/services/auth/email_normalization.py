"""Email normalization and cross-account lookup helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_GMAIL_DOMAIN = "gmail.com"
_GMAIL_DOMAINS = frozenset({_GMAIL_DOMAIN, "googlemail.com"})


def normalize_email(email: str) -> str:
    """Return a canonical form of ``email`` suitable for equality comparisons.

    Every spelling Gmail delivers to one mailbox collapses to one value: case, dots in the local part, a
    ``+tag`` suffix, and the ``googlemail.com`` alias. Other domains are only lowercased, since a dot or a
    plus can be significant there.

    Args:
        email: Raw email address as entered by a user.

    Returns:
        The normalized address."""
    normalized = email.strip().lower()
    local, _, domain = normalized.rpartition("@")
    if not local or domain not in _GMAIL_DOMAINS:
        return normalized

    mailbox = local.split("+", 1)[0].replace(".", "")
    if not mailbox:
        return normalized
    return f"{mailbox}@{_GMAIL_DOMAIN}"


def is_gmail_address(email: str) -> bool:
    """Whether ``email`` is on a domain whose mailboxes follow Gmail's addressing rules."""
    return email.strip().lower().rpartition("@")[2] in _GMAIL_DOMAINS


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


def find_verified_user_by_email(email: str) -> User | None:
    """The active account that has proved it controls ``email``: a verified primary or a verified secondary.

    Args:
        email: Raw email address.

    Returns:
        The matching User, or None.
    """
    from urbanlens.dashboard.models.profile.email import ProfileEmail
    from urbanlens.dashboard.models.profile.model import Profile

    normalized = normalize_email(email)
    if not normalized:
        return None
    profile = Profile.objects.filter(primary_email_normalized=normalized, verified_primary_email=normalized, user__is_active=True).select_related("user").first()
    if profile is not None:
        return profile.user
    secondary = ProfileEmail.objects.verified_for(normalized).filter(profile__user__is_active=True).select_related("profile__user").first()
    return secondary.profile.user if secondary is not None else None


def own_addresses(user: User) -> set[str]:
    """Every address ``user`` can be reached at, normalized: its primary and its verified secondaries."""
    from urbanlens.dashboard.models.profile.email import ProfileEmail

    addresses = set(ProfileEmail.objects.filter(profile__user=user, is_verified=True).values_list("normalized_email", flat=True))
    if user.email:
        addresses.add(normalize_email(user.email))
    return addresses


def has_verified_address(user: User, email: str) -> bool:
    """Whether ``user`` is the account that proved it controls ``email``."""
    owner = find_verified_user_by_email(email)
    return owner is not None and owner.pk == user.pk


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
