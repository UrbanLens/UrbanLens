"""WebAuthn (passkey) registration and authentication ceremonies.

Two-factor login is opt-in per account: a user with at least one saved
``WebAuthnCredential`` (or a confirmed ``TOTPDevice`` - see
``services.two_factor.has_second_factor``, the actual gate
``CustomLoginView``/``LoginTwoFactorView`` use) is routed through a challenge
after a successful password login; accounts with neither skip the step
entirely. This module only ever deals with the passkey half of that; TOTP and
backup codes live in ``services.two_factor``.

The Relying Party ID and origin are derived from the incoming request rather
than a fixed setting, so this works unmodified across local/staging/prod
hosts the same way ``request.build_absolute_uri()`` is already used elsewhere
for email links.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, parse_registration_credential_json
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidJSONStructure, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from urbanlens.dashboard.models.account import WebAuthnCredential

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

RP_NAME = "UrbanLens"
SESSION_REGISTRATION_CHALLENGE = "webauthn_registration_challenge"
SESSION_AUTHENTICATION_CHALLENGE = "webauthn_authentication_challenge"
MAX_CREDENTIALS_PER_USER = 10


class WebAuthnError(Exception):
    """Raised when a registration or authentication ceremony can't be completed."""


def _rp_id(request: HttpRequest) -> str:
    """Relying Party ID: the request host without scheme or port."""
    return request.get_host().split(":")[0]


def _origin(request: HttpRequest) -> str:
    """Origin the browser's WebAuthn ceremony must have run under."""
    return f"{request.scheme}://{request.get_host()}"


def _to_transports(values: list[str]) -> list[AuthenticatorTransport] | None:
    """Convert stored transport strings back to enum members, skipping unknown ones."""
    if not values:
        return None
    transports = []
    for value in values:
        try:
            transports.append(AuthenticatorTransport(value))
        except ValueError:
            continue
    return transports or None


def has_passkeys(user: User) -> bool:
    """True when ``user`` has enrolled at least one passkey (2FA is opt-in per user)."""
    return WebAuthnCredential.objects.filter(user=user).exists()


def list_credentials(user: User):
    """Return this user's registered passkeys, newest first."""
    return WebAuthnCredential.objects.filter(user=user)


def build_registration_options(request: HttpRequest, user: User) -> str:
    """Start a passkey-registration ceremony and return JSON options for the browser.

    Args:
        request: The incoming request (used for RP ID/origin and to stash the challenge).
        user: The account enrolling a new passkey.

    Returns:
        JSON string suitable for ``navigator.credentials.create()`` on the client.

    Raises:
        WebAuthnError: If the account has already reached the per-user credential cap.
    """
    existing = list(WebAuthnCredential.objects.filter(user=user))
    if len(existing) >= MAX_CREDENTIALS_PER_USER:
        raise WebAuthnError(f"You can register at most {MAX_CREDENTIALS_PER_USER} passkeys. Remove one first.")

    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name=RP_NAME,
        user_id=str(user.pk).encode(),
        user_name=user.username,
        user_display_name=user.get_full_name() or user.username,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(cred.credential_id), transports=_to_transports(cred.transports)) for cred in existing],
    )
    request.session[SESSION_REGISTRATION_CHALLENGE] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def verify_and_save_registration(request: HttpRequest, user: User, credential_json: str, name: str) -> WebAuthnCredential:
    """Verify a completed registration ceremony and persist the new credential.

    Args:
        request: The incoming request (holds the challenge stashed by ``build_registration_options``).
        user: The account enrolling the passkey.
        credential_json: The raw JSON produced by ``navigator.credentials.create()``'s response.
        name: A user-supplied label for the new passkey (e.g. "Bitwarden").

    Returns:
        The newly created WebAuthnCredential.

    Raises:
        WebAuthnError: If no registration is pending, the payload is malformed, or verification fails.
    """
    challenge = request.session.pop(SESSION_REGISTRATION_CHALLENGE, None)
    if not challenge:
        raise WebAuthnError("No passkey registration in progress. Please try again.")

    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
        )
        transports = [t.value for t in (parse_registration_credential_json(credential_json).response.transports or [])]
    except (InvalidRegistrationResponse, InvalidJSONStructure, KeyError, ValueError) as exc:
        logger.warning("WebAuthn registration failed for user %s: %s", user.pk, exc)
        raise WebAuthnError("That passkey could not be verified.") from exc

    if WebAuthnCredential.objects.filter(credential_id=verified.credential_id).exists():
        raise WebAuthnError("That passkey is already registered.")

    return WebAuthnCredential.objects.create(
        user=user,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        aaguid=verified.aaguid,
        backup_eligible=verified.credential_backed_up,
        device_type=verified.credential_device_type.value,
        transports=transports,
        name=(name or "").strip()[:100] or "Passkey",
    )


def build_authentication_options(request: HttpRequest, user: User) -> str:
    """Start a passkey-authentication ceremony scoped to ``user``'s registered credentials.

    Args:
        request: The incoming request (used for RP ID and to stash the challenge).
        user: The account attempting to complete login.

    Returns:
        JSON string suitable for ``navigator.credentials.get()`` on the client.

    Raises:
        WebAuthnError: If the account has no registered passkeys.
    """
    credentials = list(WebAuthnCredential.objects.filter(user=user))
    if not credentials:
        raise WebAuthnError("This account has no passkeys registered.")

    options = generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(cred.credential_id), transports=_to_transports(cred.transports)) for cred in credentials],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session[SESSION_AUTHENTICATION_CHALLENGE] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def verify_authentication(request: HttpRequest, user: User, credential_json: str) -> WebAuthnCredential:
    """Verify a completed authentication ceremony against one of ``user``'s credentials.

    Args:
        request: The incoming request (holds the challenge stashed by ``build_authentication_options``).
        user: The account attempting to complete login.
        credential_json: The raw JSON produced by ``navigator.credentials.get()``'s response.

    Returns:
        The WebAuthnCredential that was used, with its sign count/last-used timestamp updated.

    Raises:
        WebAuthnError: If no authentication is pending, the payload is malformed, the credential
            isn't registered to this user, or verification fails.
    """
    challenge = request.session.pop(SESSION_AUTHENTICATION_CHALLENGE, None)
    if not challenge:
        raise WebAuthnError("No passkey sign-in in progress. Please try again.")

    try:
        raw_id = base64url_to_bytes(json.loads(credential_json)["rawId"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise WebAuthnError("Malformed passkey response.") from exc

    try:
        stored = WebAuthnCredential.objects.get(user=user, credential_id=raw_id)
    except WebAuthnCredential.DoesNotExist as exc:
        raise WebAuthnError("That passkey is not registered to this account.") from exc

    try:
        verified = verify_authentication_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            credential_public_key=bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        logger.warning("WebAuthn authentication failed for user %s: %s", user.pk, exc)
        raise WebAuthnError("That passkey could not be verified.") from exc

    WebAuthnCredential.objects.filter(pk=stored.pk).update(
        sign_count=verified.new_sign_count,
        last_used_at=timezone.now(),
        backup_eligible=verified.credential_backed_up,
    )
    return stored
