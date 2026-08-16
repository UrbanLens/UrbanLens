"""WebAuthn (passkey) registration and authentication ceremonies.

Two-factor login is opt-in per account: a user with at least one saved
``WebAuthnCredential`` (or a confirmed ``TOTPDevice`` - see
``services.auth.two_factor.has_second_factor``, the actual gate
``CustomLoginView``/``LoginTwoFactorView`` use) is routed through a challenge
after a successful password login; accounts with neither skip the step
entirely. This module only ever deals with the passkey half of that; TOTP and
backup codes live in ``services.auth.two_factor``.

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
    """Raised when a registration or authentication ceremony can't be completed.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


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
    """True when ``user`` has enrolled at least one *login-factor* passkey.

    This is the single source of truth behind ``has_second_factor`` and every
    2FA routing decision (``CustomLoginView``, the SSO pipeline's
    ``enforce_two_factor_for_sso``). Unlock-only credentials
    (``is_login_factor=False`` - enrolled to decrypt E2EE data, see
    ``E2EEPasskeyWrap``) are deliberately excluded: adding one must never flip
    the account into mandatory 2FA.
    """
    return WebAuthnCredential.objects.for_user(user).filter(is_login_factor=True).exists()


def list_credentials(user: User):
    """Return this user's registered passkeys, newest first - both kinds.

    Settings lists everything (with a badge distinguishing sign-in factors
    from unlock-only keys); only the 2FA gates filter on ``is_login_factor``.
    """
    return WebAuthnCredential.objects.for_user(user)


def _with_prf_extension(options_json: str, *, eval_by_credential: dict[str, str] | None = None) -> str:
    """Add the WebAuthn ``prf`` extension to ceremony-options JSON.

    py_webauthn's ``options_to_json()`` emits no ``extensions`` member and its
    option dataclasses have nowhere to carry one, so the extension is spliced
    into the JSON after the fact. The client's option builders
    (``webauthn-client.ts``) understand this shape and convert the base64url
    inputs to the ``BufferSource``s the browser API wants - the same encoding
    WebAuthn Level 3's ``parseRequestOptionsFromJSON`` uses.

    Args:
        options_json: The JSON produced by ``options_to_json()``.
        eval_by_credential: Mapping of base64url credential id to base64
            32-byte PRF input, for authentication ceremonies. When None or
            empty, the extension is requested with no inputs (registration:
            asks the authenticator to *enable* PRF so a later assertion can
            evaluate it, and prompts capability detection client-side).

    Returns:
        The options JSON with the ``prf`` extension attached.
    """
    data = json.loads(options_json)
    if eval_by_credential:
        data.setdefault("extensions", {})["prf"] = {"evalByCredential": {cred_id: {"first": prf_input} for cred_id, prf_input in eval_by_credential.items()}}
    else:
        data.setdefault("extensions", {})["prf"] = {}
    return json.dumps(data)


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
    existing = list(WebAuthnCredential.objects.for_user(user))
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
    # PRF is requested on EVERY registration, not just unlock-only ones, so any
    # passkey created from now on can later gain an E2EE wrap without
    # re-registration. Authenticators that don't support it simply ignore the
    # extension; nothing else about the ceremony changes.
    return _with_prf_extension(options_to_json(options))


def verify_and_save_registration(request: HttpRequest, user: User, credential_json: str, name: str = "", *, login_factor: bool = True) -> WebAuthnCredential:
    """Verify a completed registration ceremony and persist the new credential.

    Args:
        request: The incoming request (holds the challenge stashed by ``build_registration_options``).
        user: The account enrolling the passkey.
        credential_json: The raw JSON produced by ``navigator.credentials.create()``'s response.
        name: A user-supplied label for the new passkey (e.g. "Bitwarden"). Registration no
            longer prompts for one up front, so this is normally
            empty - in that case, an auto-generated "Passkey N" name is used instead, numbered
            after the user's current passkey count. The user can still rename it afterward via
            the existing inline rename field.
        login_factor: False for keys enrolled only to unlock E2EE data - they
            are excluded from every 2FA gate (see ``has_passkeys``), so
            enrolling one never changes how the account signs in.

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

    clean_name = (name or "").strip()[:100]
    if not clean_name:
        clean_name = f"Passkey {WebAuthnCredential.objects.filter(user=user).count() + 1}"

    return WebAuthnCredential.objects.create(
        user=user,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        aaguid=verified.aaguid,
        backup_eligible=verified.credential_backed_up,
        device_type=verified.credential_device_type.value,
        transports=transports,
        name=clean_name,
        is_login_factor=login_factor,
    )


def build_authentication_options(request: HttpRequest, user: User) -> str:
    """Start a login-2FA passkey ceremony scoped to ``user``'s sign-in credentials.

    Only ``is_login_factor`` credentials are offered - an unlock-only key is
    not a second factor, by the owner's own choice. When any offered
    credential carries an ``E2EEPasskeyWrap``, its PRF input rides along in the
    assertion options, so the same biometric tap that completes 2FA also hands
    the browser the secret that unlocks the user's encrypted messages - a
    cold device gets both for zero extra prompts. The PRF output never
    reaches this server; ``verify_authentication`` neither sees nor checks it.

    Args:
        request: The incoming request (used for RP ID and to stash the challenge).
        user: The account attempting to complete login.

    Returns:
        JSON string suitable for ``navigator.credentials.get()`` on the client.

    Raises:
        WebAuthnError: If the account has no sign-in passkeys.
    """
    credentials = list(WebAuthnCredential.objects.for_user(user).filter(is_login_factor=True).select_related("e2ee_wrap__bundle"))
    if not credentials:
        raise WebAuthnError("This account has no passkeys registered.")

    options = generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(cred.credential_id), transports=_to_transports(cred.transports)) for cred in credentials],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session[SESSION_AUTHENTICATION_CHALLENGE] = bytes_to_base64url(options.challenge)

    prf_inputs: dict[str, str] = {}
    for cred in credentials:
        # getattr, not try/except: Django's RelatedObjectDoesNotExist for a
        # reverse OneToOne subclasses AttributeError precisely so this works.
        wrap = getattr(cred, "e2ee_wrap", None)
        if wrap is not None and wrap.bundle_version == wrap.bundle.version:
            prf_inputs[bytes_to_base64url(bytes(cred.credential_id))] = wrap.prf_input
    options_json = options_to_json(options)
    if prf_inputs:
        options_json = _with_prf_extension(options_json, eval_by_credential=prf_inputs)
    return options_json


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
        # is_login_factor filter: an unlock-only key was excluded from the
        # options this ceremony started from, and its owner chose for it not to
        # be a sign-in factor - an assertion from one must not complete login.
        stored = WebAuthnCredential.objects.get(user=user, credential_id=raw_id, is_login_factor=True)
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
