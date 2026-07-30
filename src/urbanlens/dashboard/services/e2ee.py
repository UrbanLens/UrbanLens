"""Server-side helpers for direct-message end-to-end encryption.

The server's entire role in the E2EE scheme is storing opaque blobs and
answering "which mode does this account authenticate in" - all cryptography
happens in the browser (see ``frontend/ts/shared/e2ee-crypto.ts`` and
``docs/e2ee.md``). Everything here is bookkeeping around that storage.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING, Any

from django.conf import settings

from urbanlens.dashboard.models.account.model import AccountKdf

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

#: Maximum accepted lengths (base64 characters) for client-supplied blobs.
#: Generous multiples of the real libsodium output sizes - anything larger is
#: garbage or abuse, not a key.
MAX_PUBLIC_KEY_LENGTH = 128
MAX_SALT_LENGTH = 64
MAX_WRAPPED_SECRET_LENGTH = 2_048
MAX_WRAPPED_CONVERSATION_KEY_LENGTH = 1_024

#: Maximum accepted length for an encrypted message body (base64). Plaintext
#: is capped at MAX_DIRECT_MESSAGE_LENGTH characters; UTF-8 + secretbox
#: overhead + base64 lands well under this.
MAX_CIPHERTEXT_LENGTH = 40_000
MAX_NONCE_LENGTH = 64

#: Authentication modes reported by the login-params endpoint.
AUTH_MODE_LEGACY = "legacy"
AUTH_MODE_DERIVED = "derived"


def is_base64(value: str) -> bool:
    """Return True when ``value`` is non-empty, well-formed standard base64.

    Args:
        value: The candidate string.

    Returns:
        True for valid base64, False for empty or malformed input.
    """
    if not value:
        return False
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def valid_blob(value: Any, max_length: int, *, required: bool = True) -> bool:
    """Validate one client-supplied base64 blob field.

    Args:
        value: The raw JSON value.
        max_length: Maximum accepted length in base64 characters.
        required: When False, empty/missing values pass.

    Returns:
        True when the value is acceptable to store.
    """
    if not value:
        return not required
    return isinstance(value, str) and len(value) <= max_length and is_base64(value)


def fake_auth_salt(identifier: str) -> str:
    """Deterministic decoy salt for identifiers with no derived-auth account.

    Real salts are 16 random bytes; this derives 16 bytes from the site secret
    and the identifier so unknown accounts are indistinguishable from enrolled
    ones (same shape, stable across requests) without maintaining any state.

    Args:
        identifier: The username or email being probed.

    Returns:
        A base64-encoded 16-byte pseudo-salt.
    """
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        f"e2ee-login-salt:{identifier.strip().lower()}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest[:16]).decode()


def group_member_token(group_uuid: Any, profile_id: int) -> str:
    """Opaque per-(group, member) identifier for the key-rotation API.

    The rotation payload used to be keyed by profile slugs, which handed every
    group member the real slug of members whose ``profile_visibility`` masks
    them elsewhere (docs/PROBLEMS.md PR #111 finding; decision 2026-07-23:
    opaque identifiers). This token is deterministic (the client round-trips
    it between GET and POST, and the server just recomputes the mapping -
    nothing is decoded), scoped to one group by the uuid in the HMAC input (so
    tokens can't correlate a member across groups), and reveals nothing about
    the member.

    Args:
        group_uuid: The group chat's UUID.
        profile_id: The member profile's pk.

    Returns:
        A hex token stable for this (group, member) pair.
    """
    return hmac.new(
        settings.SECRET_KEY.encode(),
        f"e2ee-group-member:{group_uuid}:{profile_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def resolve_login_user(identifier: str) -> User | None:
    """Find the account an identifier would log in as (username or email).

    Mirrors ``EmailOrUsernameModelBackend``'s resolution order so login-params
    answers for the same account the login POST will hit.

    Args:
        identifier: The username or email from the login form.

    Returns:
        The matching active User, or None.
    """
    from django.contrib.auth.models import User

    identifier = identifier.strip()
    if not identifier:
        return None
    user = User.objects.filter(username=identifier).first()
    if user is None and "@" in identifier:
        from urbanlens.dashboard.services.email_normalization import find_user_by_email

        user = find_user_by_email(identifier)
    return user


def login_params_for_identifier(identifier: str) -> dict[str, str]:
    """Build the login-params payload for one identifier.

    Args:
        identifier: The username or email from the login form.

    Returns:
        Dict with ``mode`` (``legacy``/``derived``) and ``auth_salt`` (real for
        enrolled accounts, deterministic decoy otherwise).
    """
    user = resolve_login_user(identifier)
    if user is not None:
        kdf = AccountKdf.objects.for_user(user).first()
        if kdf is not None:
            return {"mode": AUTH_MODE_DERIVED, "auth_salt": kdf.auth_salt}
        return {"mode": AUTH_MODE_LEGACY, "auth_salt": ""}
    return {"mode": AUTH_MODE_DERIVED, "auth_salt": fake_auth_salt(identifier)}
