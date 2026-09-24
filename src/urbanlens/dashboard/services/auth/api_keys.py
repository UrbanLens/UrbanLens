"""Generation and verification of external-application API keys.

Only a digest of a key's secret half is ever stored, and it is a bare SHA-256 rather than a password
KDF: the secret is 32 bytes of ``secrets.token_urlsafe``, so there is nothing for a work factor to
price, and PBKDF2 charged ~0.9s of CPU to every request naming a real (public) prefix. Keys issued
before that change still verify through ``check_password`` and are rewritten on first use - see P146.
User passwords are untouched and still go through ``PASSWORD_HASHERS``.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import secrets
from typing import TYPE_CHECKING

from django.contrib.auth.hashers import check_password
from django.db import IntegrityError, transaction
from django.db.models import Q
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

#: Tags a ``key_hash`` in the current format and versions it. No Django hasher's algorithm name
#: can be ``ulk1``, so one ``startswith`` separates the two namespaces.
_HASH_SCHEME = "ulk1$"

#: Shortest secret the bare digest will accept, at issuance and at verification: the case for an
#: unsalted single pass rests on the secret being CSPRNG output. ``token_urlsafe(32)`` is 43 characters.
MIN_FAST_HASH_SECRET_LENGTH = 40

#: How stale ``last_used_at`` may get. Writing it on every request made one row the lock every
#: concurrent request on a key queued behind (measured in P146).
LAST_USED_RESOLUTION = timedelta(minutes=1)

#: API keys listed per page in Settings > Advanced > API Keys.
API_KEYS_PAGE_SIZE = 10

#: Its own page parameter, since the settings page hosts several paginated sections.
API_KEYS_PAGE_PARAM = "api_keys_page"


def generate_api_key(user: User, name: str) -> tuple[ApiKey, str]:
    """Create a new API key for ``user`` and return it with its one-time plaintext.

    Args:
        user: The account the key acts on behalf of.
        name: User-facing label (e.g. "Zapier").

    Returns:
        Tuple of (the new ``ApiKey`` row, the raw key string).

    Raises:
        RuntimeError: Every attempt drew a prefix that was already taken (should never happen in practice)."""
    # sanitize_name strips control characters (including NUL, which Postgres
    # rejects outright) and markup-significant characters - this label is
    # rendered back in the settings page same as any other user-facing name.
    cleaned_name = (sanitize_name(name) or "").strip()[:100] or "API Key"
    secret = secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)
    key_hash = _hash_secret(secret)
    # The unique prefix column decides a collision, so the insert is what is retried.
    for _ in range(5):
        prefix = secrets.token_urlsafe(8)[:_PREFIX_LENGTH]
        try:
            with transaction.atomic():
                api_key = ApiKey.objects.create(user=user, name=cleaned_name, prefix=prefix, key_hash=key_hash)
        except IntegrityError:
            continue
        # No separator between prefix and secret: token_urlsafe's alphabet includes "_", so a
        # delimiter-based split could misparse a randomly generated prefix/secret that happens to
        # contain one. Fixed-length slicing in authenticate_api_key recovers the boundary.
        return api_key, f"{KEY_LABEL}_{prefix}{secret}"
    raise RuntimeError("Failed to generate a unique API key prefix.")


def _hash_secret(secret: str) -> str:
    """Encode ``secret`` for ``ApiKey.key_hash``.

    Args:
        secret: The secret half of a key.

    Returns:
        The scheme-tagged hex digest.

    Raises:
        ValueError: ``secret`` is shorter than :data:`MIN_FAST_HASH_SECRET_LENGTH`.
    """
    if len(secret) < MIN_FAST_HASH_SECRET_LENGTH:
        raise ValueError(f"An API key secret must be at least {MIN_FAST_HASH_SECRET_LENGTH} characters; got {len(secret)}.")
    return _HASH_SCHEME + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_api_key_secret(secret: str, encoded: str) -> tuple[bool, bool]:
    """Check a presented secret against a stored ``key_hash``, in either format.

    Cheap for a current-format row. A legacy row costs a full PBKDF2, which is why the WebSocket path
    runs this off the shared database thread.

    Args:
        secret: The secret half of the presented key.
        encoded: The row's stored ``key_hash``.

    Returns:
        ``(is_correct, is_legacy)``; ``is_legacy`` means a correct secret should be rewritten with
        :func:`finish_api_key_authentication`.
    """
    if not encoded.startswith(_HASH_SCHEME):
        # check_password returns False for an unidentifiable algorithm but raises for an identifiable
        # one with a malformed body (a truncated "pbkdf2_sha256$..."), and a corrupt row must deny, not 500.
        try:
            return check_password(secret, encoded), True
        except (ValueError, TypeError):
            return False, True

    if len(secret) < MIN_FAST_HASH_SECRET_LENGTH:
        return False, False
    expected = hashlib.sha256(secret.encode("utf-8")).digest()
    try:
        stored = bytes.fromhex(encoded[len(_HASH_SCHEME) :])
    except ValueError:
        return False, False
    # Bytes, not hex text: compare_digest raises TypeError on a non-ASCII str.
    return secrets.compare_digest(expected, stored), False


def _upgrade_key_hash(api_key: ApiKey, secret: str) -> bool:
    """Rewrite a just-verified legacy ``key_hash`` in the current format.

    A compare-and-swap against the value this request read, so a racing request that already
    upgraded the row, or a reissue in between, is never overwritten.

    Args:
        api_key: The row that just authenticated, still carrying the ``key_hash`` it was read with.
        secret: The verified secret half.

    Returns:
        True if this call rewrote the row.
    """
    upgraded = _hash_secret(secret)
    changed = ApiKey.objects.filter(pk=api_key.pk, key_hash=api_key.key_hash).update(key_hash=upgraded)
    if changed:
        api_key.key_hash = upgraded
    return bool(changed)


def finish_api_key_authentication(api_key: ApiKey, secret: str, *, is_legacy: bool) -> ApiKey:
    """The database writes owed by a key whose secret has just verified.

    Args:
        api_key: The verified row.
        secret: Its verified secret half.
        is_legacy: From :func:`verify_api_key_secret`.

    Returns:
        ``api_key``, with ``usage_sample`` set.
    """
    if is_legacy:
        _upgrade_key_hash(api_key, secret)
    api_key.usage_sample = touch_api_key(api_key)
    return api_key


def authenticate_api_key(raw_key: str) -> ApiKey | None:
    """Resolve a presented raw key to its ``ApiKey`` row, or None if invalid.

    Nothing about the decision is cached, so a revocation takes effect on the very next request.

    Args:
        raw_key: The full presented key, e.g. the ``Authorization`` header's token part after ``Bearer ``.

    Returns:
        The matching, non-revoked ``ApiKey`` if the secret checks out; None
        for a malformed, unknown, revoked, or mismatched key.
    """
    candidate = api_key_candidate(raw_key)
    if candidate is None:
        # Hash anyway, so an unknown prefix costs what a known one does.
        hashlib.sha256(raw_key.encode("utf-8")).digest()
        return None
    api_key, secret = candidate
    is_correct, is_legacy = verify_api_key_secret(secret, api_key.key_hash)
    if not is_correct:
        return None
    return finish_api_key_authentication(api_key, secret, is_legacy=is_legacy)


def api_key_candidate(raw_key: str) -> tuple[ApiKey, str] | None:
    """The row a presented key *claims* to be, and the secret still to check.

    The lookup half of :func:`authenticate_api_key`, split out so the WebSocket path can run the
    database work and the verification on different threads (see ``websocket_auth.ApiKeyAuthMiddleware``).

    Args:
        raw_key: The full presented key.

    Returns:
        ``(row, secret)`` for a well-formed key naming a live row, else None.
        **A returned row is not authenticated** - the secret has not been
        checked yet, and a caller that skips that check has authenticated
        nothing.
    """
    label_prefix = f"{KEY_LABEL}_"
    if not raw_key.startswith(label_prefix):
        return None
    remainder = raw_key[len(label_prefix) :]
    if len(remainder) <= _PREFIX_LENGTH:
        return None
    prefix, secret = remainder[:_PREFIX_LENGTH], remainder[_PREFIX_LENGTH:]

    api_key = ApiKey.objects.active().filter(prefix=prefix, user__is_active=True).select_related("user", "user__profile").first()
    return (api_key, secret) if api_key is not None else None


def touch_api_key(api_key: ApiKey) -> bool:
    """Refresh ``last_used_at``, at most once per key per :data:`LAST_USED_RESOLUTION`.

    The window is re-tested in SQL, so concurrent workers that read the same stale row collapse to one
    write and the rest match no row, taking no lock.

    Args:
        api_key: The row that passed its secret check. Stamped with now in memory either way.

    Returns:
        Whether this call wrote - once per key per window, deployment-wide.
    """
    now = timezone.now()
    cutoff = now - LAST_USED_RESOLUTION
    previous = api_key.last_used_at
    api_key.last_used_at = now
    if previous is not None and previous >= cutoff:
        return False
    stale = Q(last_used_at__isnull=True) | Q(last_used_at__lt=cutoff)
    return bool(ApiKey.objects.filter(pk=api_key.pk).filter(stale).update(last_used_at=now))


def record_api_key_usage(api_key: ApiKey, endpoint: str) -> None:
    """Log one activity entry for ``api_key``, trimming older entries beyond ``USAGE_LOG_LIMIT``.

    Called only for successfully authenticated requests, and for reads only on the one request per
    :data:`LAST_USED_RESOLUTION` that refreshed ``last_used_at`` (see
    ``external_api.authentication.ApiKeyAuthentication.authenticate``), so the trail is every write and
    a once-a-minute sample of reads. Never called for a rejected key, so probing cannot grow or mine it.

    Args:
        api_key: The key that was just used to authenticate a request.
        endpoint: The request path that was called, e.g. ``request.path``."""
    ApiKeyUsageLog.objects.create(api_key=api_key, endpoint=endpoint)
    stale_ids = list(ApiKeyUsageLog.objects.for_api_key(api_key).order_by("-created").values_list("pk", flat=True)[USAGE_LOG_LIMIT:])
    if stale_ids:
        ApiKeyUsageLog.objects.filter(pk__in=stale_ids).delete()


def revoke_api_key(user: User, api_key_id: int) -> bool:
    """Revoke one of ``user``'s API keys, if it exists and isn't already revoked.

    Args:
        user: The owner - scoping by user prevents revoking someone else's key by guessing an id.
        api_key_id: Primary key of the ``ApiKey`` row to revoke.

    Returns:
        True if a key was revoked, False if no matching active key existed."""
    updated = ApiKey.objects.for_user(user).active().filter(pk=api_key_id).update(revoked_at=timezone.now())
    return updated > 0


def active_api_key_count(user: User) -> int:
    """How many of ``user``'s API keys still work.

    Args:
        user: The owner.

    Returns:
        The number of unrevoked keys.
    """
    return ApiKey.objects.for_user(user).active().count()


def revoke_all_api_keys(user: User) -> int:
    """Revoke every one of ``user``'s active API keys at once.
    Revoked, not deleted: ``ApiKeyUsageLog`` rows hang off the key, and the settings page shows a revoked key so its owner can see it went away.

    Args:
        user: The owner.

    Returns:
        How many keys this revoked."""
    return ApiKey.objects.for_user(user).active().update(revoked_at=timezone.now())


def api_keys_settings_context(user: User, request: HttpRequest, **extra: object) -> dict:
    """Context for the Security section's API Keys subsection.
    Pops ``new_api_key`` from the session, which ``ApiKeyCreateView`` stashes there as a one-time flash: the plaintext key is only ever available on the response immediately after generating it.

    Args:
        user: The account whose API keys to list.
        request: The current request (used for the session and for building absolute endpoint URLs).
        **extra: Additional context to merge in.

    Returns:
        Context dict with ``api_keys`` (one page of them, working keys first and newest first within each group, each with its ``usage_log`` prefetched), ``api_keys_page_obj``, ``new_api_key``, ``external_api_whoami_url``, and ``external_api_pins_url``."""
    from django.urls import reverse

    from urbanlens.dashboard.services.core.pagination import get_page

    # Revoked keys stay listed on purpose (see `revoke_all_api_keys`), so this list only ever grows
    # and has to page rather than be trimmed.
    # Working keys sort first because they are the ones with an action attached: an account that had
    # revoked a page's worth of keys would otherwise have to page forward to reach the key it
    keys = ApiKey.objects.for_user(user).alias(still_working=Q(revoked_at__isnull=True)).order_by("-still_working", "-created").prefetch_related("usage_log")
    page = get_page(request, keys, API_KEYS_PAGE_SIZE, param=API_KEYS_PAGE_PARAM)
    return {
        "api_keys": list(page.object_list),
        "api_keys_page_obj": page,
        "new_api_key": request.session.pop("new_api_key", None),
        "external_api_whoami_url": request.build_absolute_uri(reverse("external_api:whoami")),
        "external_api_pins_url": request.build_absolute_uri(reverse("external_api:pins")),
        **extra,
    }
