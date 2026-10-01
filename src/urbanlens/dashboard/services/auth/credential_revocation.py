"""What a password change revokes, decided in one place for every path that changes a password.

Browser sessions need nothing here: Django signs each session with a hash of the password, so every other
session stops authenticating the moment the hash changes, and ``update_session_auth_hash`` re-signs the one
that made the change. Open WebSockets re-check the same hash on a timer (``consumers.CredentialScopeMixin``).
What the hash does not reach is delegated access: OAuth2 tokens and API keys, which is what this module
revokes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import TYPE_CHECKING

from django.contrib.auth import update_session_auth_hash
from django.db import transaction

from urbanlens.dashboard.services.auth.api_keys import revoke_all_api_keys

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


class PasswordChangeKind(StrEnum):
    """How the password changed, which decides what else is revoked with it."""

    #: The emailed reset link. Nobody is signed in, so no session is kept.
    RESET = "reset"
    #: A signed-in change, or an SSO-only account setting its first password.
    CHANGE = "change"
    #: A staff member setting the password through Django admin.
    ADMIN = "admin"
    #: The same password re-derived into a new stored form (E2EE enrollment). Nobody gained or lost knowledge
    #: of the secret, so delegated access is left alone.
    REENCODE = "reencode"

    @property
    def revokes_delegated_access(self) -> bool:
        """Whether OAuth2 grants end with this change, and whether API keys may be revoked with it."""
        return self is not PasswordChangeKind.REENCODE


@dataclass(frozen=True)
class RevokedCredentials:
    """What one password change revoked."""

    oauth_tokens: int = 0
    api_keys: int = 0


def revoke_credentials_on_password_change(
    user: User,
    *,
    kind: PasswordChangeKind,
    request: HttpRequest | None = None,
    revoke_api_keys: bool = False,
) -> RevokedCredentials:
    """Revoke what should not outlive ``user``'s new password, keeping the requester's own session.

    Call after the new password is saved, in the same transaction.

    API keys are revoked only when the owner (or an admin acting for them) asks: a key usually lives in a script
    or a server the owner set up by hand, so revoking it by default breaks things a routine password change had
    no reason to touch. OAuth2 grants end unconditionally, because an app re-authorizes with the new password in
    one sign-in.

    Args:
        user: The account whose password just changed.
        kind: How it changed.
        request: The request that changed it. When it carries ``user``'s own session, that session is re-signed
            so it stays signed in.
        revoke_api_keys: Whether to revoke every API key too. Ignored for ``REENCODE``.

    Returns:
        Counts of what was revoked.
    """
    if request is not None and getattr(request.user, "pk", None) == user.pk and getattr(request, "session", None) is not None and request.session.session_key:
        update_session_auth_hash(request, user)

    if not kind.revokes_delegated_access:
        return RevokedCredentials()

    with transaction.atomic():
        oauth_tokens = revoke_oauth_grants(user)
        api_keys = revoke_all_api_keys(user) if revoke_api_keys else 0
    logger.info("Password %s for user %s revoked %d OAuth2 token(s) and %d API key(s)", kind.value, user.pk, oauth_tokens, api_keys)
    return RevokedCredentials(oauth_tokens=oauth_tokens, api_keys=api_keys)


def revoke_oauth_grants(user: User) -> int:
    """End every OAuth2 grant ``user`` has given: tokens, ID tokens, and authorization and device codes.

    Refresh tokens are deleted rather than marked revoked, because DOT honours a revoked refresh token for
    ``REFRESH_TOKEN_GRACE_PERIOD_SECONDS`` after revocation.

    Args:
        user: The resource owner.

    Returns:
        How many access and refresh tokens were removed.
    """
    from oauth2_provider.models import get_access_token_model, get_device_grant_model, get_grant_model, get_id_token_model, get_refresh_token_model

    refresh_deleted, _ = get_refresh_token_model().objects.filter(user=user).delete()
    access_deleted, _ = get_access_token_model().objects.filter(user=user).delete()
    get_id_token_model().objects.filter(user=user).delete()
    get_grant_model().objects.filter(user=user).delete()
    get_device_grant_model().objects.filter(user=user).delete()
    return refresh_deleted + access_deleted
