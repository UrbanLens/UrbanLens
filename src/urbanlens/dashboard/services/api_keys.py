"""Generation and verification of external-application API keys.

Mirrors the hash-never-store-plaintext pattern used for backup codes
(``services.two_factor``): the plaintext key exists only at generation time,
long enough to hand back to the caller once, and every later check compares
against a salted hash rather than the raw value.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyUsageLog
from urbanlens.dashboard.services.locations.naming import sanitize_name

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

#: Prefixes every issued key so it's visually identifiable (in logs, in a
#: pasted support message) as an UrbanLens external-API credential.
KEY_LABEL = "ulk"
_PREFIX_LENGTH = 10
_SECRET_ENTROPY_BYTES = 32

#: Most recent activity entries kept per key - see record_api_key_usage. This
#: is a "does this look right to me" sanity check for the key's owner, not a
#: compliance-grade audit log, so an unbounded table isn't worth the upkeep.
USAGE_LOG_LIMIT = 20


def generate_api_key(user: User, name: str) -> tuple[ApiKey, str]:
    """Create a new API key for ``user`` and return it with its one-time plaintext.

    Args:
        user: The account the key acts on behalf of.
        name: User-facing label (e.g. "Zapier"). Falls back to "API Key" if blank.

    Returns:
        Tuple of (the new ``ApiKey`` row, the raw key string). The raw key is
        never recoverable again once this function returns - only its hash is
        persisted.

    Raises:
        RuntimeError: A unique key prefix couldn't be generated (should never
            happen in practice - see the retry loop below).
    """
    # Collisions are astronomically unlikely (10 url-safe chars is ~59 bits of
    # entropy) but the prefix is a unique DB column, so retry defensively
    # instead of ever surfacing an IntegrityError to the caller.
    prefix = ""
    for _ in range(5):
        candidate = secrets.token_urlsafe(8)[:_PREFIX_LENGTH]
        if not ApiKey.objects.filter(prefix=candidate).exists():
            prefix = candidate
            break
    else:
        raise RuntimeError("Failed to generate a unique API key prefix.")

    secret = secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)
    # No separator between prefix and secret: token_urlsafe's alphabet
    # includes "_", so a delimiter-based split could misparse a randomly
    # generated prefix/secret that happens to contain one. Fixed-length
    # slicing in authenticate_api_key recovers the boundary unambiguously.
    raw_key = f"{KEY_LABEL}_{prefix}{secret}"
    # sanitize_name strips control characters (including NUL, which Postgres
    # rejects outright) and markup-significant characters - this label is
    # rendered back in the settings page same as any other user-facing name.
    cleaned_name = (sanitize_name(name) or "").strip()[:100]
    api_key = ApiKey.objects.create(
        user=user,
        name=cleaned_name or "API Key",
        prefix=prefix,
        key_hash=make_password(secret),
    )
    return api_key, raw_key


def authenticate_api_key(raw_key: str) -> ApiKey | None:
    """Resolve a presented raw key to its ``ApiKey`` row, or None if invalid.

    Looks the key up by its public prefix first (cheap, indexed) before
    hashing the secret half, rather than iterating every active key's hash -
    see :class:`~urbanlens.dashboard.models.account.model.ApiKey`'s docstring
    for why that matters here specifically.

    Args:
        raw_key: The full presented key, e.g. the ``Authorization`` header's
            token part after ``Bearer ``.

    Returns:
        The matching, non-revoked ``ApiKey`` if the secret checks out; None
        for a malformed, unknown, revoked, or mismatched key.
    """
    label_prefix = f"{KEY_LABEL}_"
    if not raw_key.startswith(label_prefix):
        return None
    remainder = raw_key[len(label_prefix) :]
    if len(remainder) <= _PREFIX_LENGTH:
        return None
    prefix, secret = remainder[:_PREFIX_LENGTH], remainder[_PREFIX_LENGTH:]

    api_key = ApiKey.objects.active().filter(prefix=prefix, user__is_active=True).select_related("user", "user__profile").first()
    if api_key is None or not check_password(secret, api_key.key_hash):
        return None

    ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
    return api_key


def record_api_key_usage(api_key: ApiKey, endpoint: str) -> None:
    """Log one activity entry for ``api_key``, trimming older entries beyond ``USAGE_LOG_LIMIT``.

    Called only for successfully authenticated requests (see
    ``external_api.authentication.ApiKeyAuthentication.authenticate``) - never
    for a rejected/unresolved key, so this table can't be grown or mined by
    probing with invalid keys.

    Args:
        api_key: The key that was just used to authenticate a request.
        endpoint: The request path that was called, e.g. ``request.path``.
    """
    ApiKeyUsageLog.objects.create(api_key=api_key, endpoint=endpoint)
    stale_ids = list(ApiKeyUsageLog.objects.for_api_key(api_key).order_by("-created").values_list("pk", flat=True)[USAGE_LOG_LIMIT:])
    if stale_ids:
        ApiKeyUsageLog.objects.filter(pk__in=stale_ids).delete()


def revoke_api_key(user: User, api_key_id: int) -> bool:
    """Revoke one of ``user``'s API keys, if it exists and isn't already revoked.

    Args:
        user: The owner - scoping by user prevents revoking someone else's key
            by guessing an id.
        api_key_id: Primary key of the ``ApiKey`` row to revoke.

    Returns:
        True if a key was revoked, False if no matching active key existed.
    """
    updated = ApiKey.objects.for_user(user).active().filter(pk=api_key_id).update(revoked_at=timezone.now())
    return updated > 0


def api_keys_settings_context(user: User, request: HttpRequest, **extra: object) -> dict:
    """Context for the Security section's API Keys subsection.

    Shared by the full settings page render and ``ApiKeyCreateView``/``ApiKeyRevokeView``,
    which re-render just the Security section for an htmx request after a
    mutation - mirrors ``services.two_factor.security_settings_context``.

    Pops ``new_api_key`` from the session, which ``ApiKeyCreateView`` stashes
    there as a one-time flash: the plaintext key is only ever available on the
    response immediately after generating it.

    Also builds the real, host-correct URLs for the two external API
    endpoints (dev/prod both resolve correctly) so the settings page can show
    a copy-pasteable "how to use this key" example instead of leaving the
    user to find the routes in source.

    Args:
        user: The account whose API keys to list.
        request: The current request (used for the session and for building
            absolute endpoint URLs).
        **extra: Additional context to merge in.

    Returns:
        Context dict with ``api_keys`` (newest first, revoked included, each
        with its ``usage_log`` prefetched), ``new_api_key``,
        ``external_api_whoami_url``, and ``external_api_pins_url``.
    """
    from django.urls import reverse

    return {
        "api_keys": list(ApiKey.objects.for_user(user).order_by("-created").prefetch_related("usage_log")),
        "new_api_key": request.session.pop("new_api_key", None),
        "external_api_whoami_url": request.build_absolute_uri(reverse("external_api:whoami")),
        "external_api_pins_url": request.build_absolute_uri(reverse("external_api:pins")),
        **extra,
    }
