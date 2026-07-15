"""Account-wide 2FA orchestration: which factors an account has, and backup codes.

Two independent second factors are supported - passkeys (``services.webauthn``)
and an authenticator app (TOTP, RFC 6238) - either is sufficient to satisfy
the login gate in ``CustomLoginView``/``LoginTwoFactorView``. Backup codes are
not a factor of their own; they're a recovery mechanism that only makes sense
once at least one real factor is enabled, and they're cleared automatically
once an account no longer has any (see ``maybe_clear_backup_codes``).
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
import pyotp

from urbanlens.dashboard.models.account import BackupCode, TOTPDevice
from urbanlens.dashboard.services.webauthn import has_passkeys

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

TOTP_ISSUER = "UrbanLens"
BACKUP_CODE_COUNT = 10
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - avoids transcription errors
BACKUP_CODE_LENGTH = 10  # rendered as two 5-char groups, e.g. "AB3XZ-9KLMN"
SESSION_PENDING_TOTP_SECRET = "pending_totp_secret"  # noqa: S105 - session key, not a credential

# Session keys for a password-verified (or SSO-verified) login that's paused
# pending a second factor. Shared between CustomLoginView (password login,
# account.py), LoginTwoFactorView and friends (the challenge page itself,
# account.py), and the social-auth pipeline step below (SSO login) - all
# three need to agree on where the pending user id/redirect live.
SESSION_WEBAUTHN_PENDING_USER = "webauthn_pending_user_id"
SESSION_WEBAUTHN_PENDING_REDIRECT = "webauthn_pending_redirect"


# -- Account-wide status -----------------------------------------------------


def has_totp(user: User) -> bool:
    """True when the account has a confirmed authenticator-app device."""
    return TOTPDevice.objects.filter(user=user).exists()


def has_second_factor(user: User) -> bool:
    """True when the account has any second factor (passkey or TOTP) enabled.

    This is the single gate ``CustomLoginView``/``LoginTwoFactorView`` use to
    decide whether a password login needs a follow-up challenge.
    """
    return has_passkeys(user) or has_totp(user)


def security_settings_context(user: User, request: HttpRequest, **extra: object) -> dict:
    """Context for Settings > Security: passkeys, TOTP status, backup codes.

    Shared by the full settings page render and every 2FA action view
    (``TOTPSetupStartView`` etc.) so an htmx request can re-render just the
    security section with fully up-to-date state after a mutation, instead
    of falling back to a full page redirect.

    Also pops ``new_backup_codes`` from the session, which
    ``BackupCodesGenerateView`` stashes there as a one-time flash - the
    plaintext codes are only ever available on the response immediately
    after generating them.

    Args:
        user: The account whose security state to describe.
        request: The current request (used for the session).
        **extra: Additional context to merge in, e.g. ``code_error`` for an
            inline TOTP-confirm failure message.
    """
    from urbanlens.dashboard.services.webauthn import list_credentials

    return {
        "passkeys": list_credentials(user),
        "has_totp": has_totp(user),
        "pending_totp_secret": request.session.get(SESSION_PENDING_TOTP_SECRET),
        "backup_code_count": remaining_backup_code_count(user),
        "new_backup_codes": request.session.pop("new_backup_codes", None),
        **extra,
    }


def maybe_clear_backup_codes(user: User) -> None:
    """Delete this account's backup codes once it has no second factor left.

    Call after removing a passkey or disabling TOTP - backup codes with
    nothing left to back up are just dead, potentially-leaked secrets.
    """
    if not has_second_factor(user):
        BackupCode.objects.filter(user=user).delete()


# -- TOTP (authenticator app) -------------------------------------------------


def generate_totp_secret() -> str:
    """Return a fresh random base32 TOTP secret."""
    return pyotp.random_base32()


def totp_provisioning_uri(user: User, secret: str) -> str:
    """Return the ``otpauth://`` URI an authenticator app's QR scanner expects."""
    label = user.email or user.username
    return pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=TOTP_ISSUER)


def _totp_matched_step(secret: str, code: str, valid_window: int = 1) -> int | None:
    """Return the time-step ``code`` matches against ``secret``, or None.

    Reimplements ``pyotp.TOTP.verify()``'s window search by hand (rather than
    calling it directly) so the caller learns *which* step matched - needed
    for replay protection, which ``verify()`` alone can't provide.
    """
    totp = pyotp.TOTP(secret)
    # Some authenticator apps display/copy the code with a middle space
    # (e.g. "123 456") - strip all whitespace, not just the ends, so a
    # pasted code still matches. Client-side JS does the same, but this is
    # the authoritative check and must not depend on JS having run.
    normalized = "".join(code.split())
    current_step = int(time.time() / totp.interval)
    for step in range(current_step - valid_window, current_step + valid_window + 1):
        if secrets.compare_digest(totp.generate_otp(step), normalized):
            return step
    return None


def enroll_totp(user: User, secret: str) -> TOTPDevice:
    """Persist a confirmed TOTP secret as this account's authenticator device.

    Args:
        user: The account enrolling.
        secret: The base32 secret the user just confirmed with a live code.

    Returns:
        The created TOTPDevice.
    """
    return TOTPDevice.objects.create(user=user, secret=secret)


def disable_totp(user: User) -> None:
    """Remove this account's TOTP device, then drop backup codes if that was the last factor."""
    TOTPDevice.objects.filter(user=user).delete()
    maybe_clear_backup_codes(user)


def verify_totp_code(user: User, code: str) -> bool:
    """Verify a submitted code against the account's TOTP device, if any.

    Rejects reuse of a previously-accepted time-step (replay protection) and
    persists the newly-used step on success.

    Args:
        user: The account attempting to verify.
        code: The 6-digit code the user typed in.

    Returns:
        True if the code is valid and freshly-used; False otherwise (including
        when the account has no TOTP device).
    """
    device = TOTPDevice.objects.filter(user=user).first()
    if device is None or not code:
        return False

    step = _totp_matched_step(device.secret, code)
    if step is None:
        return False
    if device.last_used_step is not None and step <= device.last_used_step:
        return False

    TOTPDevice.objects.filter(pk=device.pk).update(last_used_step=step)
    return True


def verify_totp_setup_code(secret: str, code: str) -> bool:
    """Verify a code against a not-yet-persisted secret, during enrollment."""
    return _totp_matched_step(secret, code) is not None


# -- Backup codes --------------------------------------------------------------


def _format_backup_code(raw: str) -> str:
    midpoint = len(raw) // 2
    return f"{raw[:midpoint]}-{raw[midpoint:]}"


def _normalize_backup_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


def generate_backup_codes(user: User) -> list[str]:
    """Replace this account's backup codes with a fresh set and return them in plaintext.

    The plaintext is only ever available here, at generation time - only
    salted hashes are persisted. Call site is responsible for showing these
    to the user exactly once.

    Args:
        user: The account generating new codes.

    Returns:
        The new codes, formatted for display (e.g. "AB3XZ-9KLMN").
    """
    BackupCode.objects.filter(user=user).delete()
    codes = ["".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH)) for _ in range(BACKUP_CODE_COUNT)]
    BackupCode.objects.bulk_create(BackupCode(user=user, code_hash=make_password(raw)) for raw in codes)
    return [_format_backup_code(raw) for raw in codes]


def remaining_backup_code_count(user: User) -> int:
    """Count of this account's not-yet-used backup codes."""
    return BackupCode.objects.filter(user=user, used_at__isnull=True).count()


def verify_and_consume_backup_code(user: User, code: str) -> bool:
    """Check a submitted backup code and mark it used if it matches.

    Args:
        user: The account attempting to verify.
        code: The user-typed code, with or without formatting punctuation.

    Returns:
        True if an unused code matched (and has now been consumed); False otherwise.
    """
    normalized = _normalize_backup_code(code)
    if not normalized:
        return False
    for candidate in BackupCode.objects.filter(user=user, used_at__isnull=True):
        if check_password(normalized, candidate.code_hash):
            BackupCode.objects.filter(pk=candidate.pk).update(used_at=timezone.now())
            return True
    return False


# -- Combined login-time verification ------------------------------------------


def verify_login_code(user: User, code: str) -> bool:
    """Verify a login-time code as either a TOTP code or a backup code.

    Used by ``LoginTwoFactorCodeView`` as the fallback to a passkey assertion -
    tries TOTP first (if a device exists), then falls back to backup codes,
    since either is an acceptable second factor at login.
    """
    return verify_totp_code(user, code) or verify_and_consume_backup_code(user, code)
