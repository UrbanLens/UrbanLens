"""Resolve what a person types to name an account - a username or an address, in any equivalent spelling."""

from __future__ import annotations

from django.contrib.auth.models import User

from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email, normalize_email
from urbanlens.dashboard.services.auth.username import find_user_by_username, normalize_username_key


def find_user_by_identifier(identifier: str, *, active_only: bool = True) -> User | None:
    """The account an identifier names, the way the login form reads it.

    An exact username wins, whatever the account's state, so the auth backend still reports an inactive
    account as such. Otherwise an address matches the account's primary or a verified secondary in any
    spelling ``normalize_email`` folds together, and anything else is read as a username in any spelling
    ``normalize_username_key`` folds together.

    Args:
        identifier: A username or an email address, as typed.
        active_only: When True, only active accounts match past the exact-username step.

    Returns:
        The matching User, or None.
    """
    identifier = identifier.strip()
    if not identifier:
        return None
    exact = User.objects.filter(username=identifier).first()
    if exact is not None:
        return exact
    if "@" in identifier:
        return find_user_by_email(identifier, active_only=active_only)
    return find_user_by_username(identifier, active_only=active_only)


def canonical_identifier(identifier: str) -> str:
    """One string for every spelling of an identifier, whether or not an account holds it.

    Keys per-identifier state that must not tell spellings apart - a lockout counter or a decoy salt -
    when there is no account to key it by instead.

    Args:
        identifier: A username or an email address, as typed.

    Returns:
        The normalized address, or the username key, or the lowercased input when neither leaves anything.
    """
    stripped = identifier.strip()
    if "@" in stripped:
        return normalize_email(stripped)
    return normalize_username_key(stripped) or stripped.lower()
