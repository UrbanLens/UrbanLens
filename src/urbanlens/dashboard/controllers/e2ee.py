"""Endpoints for direct-message end-to-end encryption key storage.

Every blob accepted here was encrypted client-side; these views only validate
shape, enforce ownership, and store. See ``docs/designs/e2ee.md`` for the scheme and
``services/e2ee.py`` for the shared helpers.

These are *dual-auth* endpoints (see
``external_api.mixins.DualAuthJsonView``): the same URL serves the site's own
web client over a session cookie and the mobile client over an OAuth2 access
token. There is deliberately one implementation rather than a session copy
here and a credential copy under ``api/external/`` - the key-exchange contract
(version races, the opaque group-member tokens, the exact 409 bodies clients
retry on) is delicate enough that two copies would drift, and a drift here
means someone's messages stop decrypting.

Because they are ``APIView`` subclasses, ``authentication_classes`` /
``permission_classes`` / ``throttle_classes`` are actually honored - on a
plain Django ``View`` those attributes are inert decoration, and an endpoint
carrying them would look converted while remaining session-only.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ParseError
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.controllers import e2ee_schema
from urbanlens.dashboard.external_api.errors import MALFORMED_JSON_BODY_MESSAGE
from urbanlens.dashboard.external_api.mixins import DualAuthJsonView
from urbanlens.dashboard.models.account.model import AccountKdf, ApiKeyScope, WebAuthnCredential
from urbanlens.dashboard.models.e2ee import ConversationKey, E2EEPasskeyWrap, MessagingKeyBundle
from urbanlens.dashboard.models.e2ee.key_bundle import DEFAULT_KDF_MEMLIMIT, DEFAULT_KDF_OPSLIMIT
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message
from urbanlens.dashboard.services.security.e2ee import (
    MAX_PUBLIC_KEY_LENGTH,
    MAX_SALT_LENGTH,
    MAX_WRAPPED_CONVERSATION_KEY_LENGTH,
    MAX_WRAPPED_SECRET_LENGTH,
    login_params_for_identifier,
    valid_blob,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

#: Confirmation string the reset endpoint requires, to make the destructive
#: consequence (old encrypted messages become unreadable) an explicit choice.
RESET_CONFIRMATION = "RESET"


def _get_profile(request: HttpRequest | Request) -> Profile:
    """Return (creating if needed) the requesting user's profile.

    Args:
        request: The authenticated request.

    Returns:
        The user's Profile.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _json_body(request: HttpRequest | Request) -> dict[str, Any] | None:
    """Parse the request body as a JSON object.

    Args:
        request: The incoming request. A DRF request has already parsed and
            validated the body by the time a handler runs, so its ``.data`` is
            reused rather than re-reading (and re-decoding) ``.body``; the one
            remaining plain-Django view here has no ``.data`` and still parses
            for itself.

    Returns:
        The parsed dict, or None when the body is not a JSON object.
    """
    # Tested with isinstance rather than ``hasattr(request, "data")``: reading
    # that attribute is what *runs* the parser, and hasattr only suppresses
    # AttributeError - a malformed body would raise ParseError straight out of
    # the probe, before the try block below could catch it, and the caller
    # would answer with DRF's ``{"detail": ...}`` instead of this module's
    # ``{"error": ...}``.
    if isinstance(request, Request):
        try:
            data = request.data
        except ParseError:
            # Malformed JSON. Returned as None so the caller answers with this
            # module's own ``{"error": ...}`` 400 rather than DRF's
            # ``{"detail": ...}`` - the status is the same either way, but the
            # body shape is part of what clients already parse.
            #
            # Reached for session callers only. django-oauth-toolkit's
            # authenticator inspects the request body while verifying a token,
            # so for a *credential* caller a malformed body raises ParseError
            # during authentication and DRF answers with ``{"detail": ...}``
            # before this handler is entered. Still a JSON 400 either way; the
            # web client's exact contract is what this branch preserves.
            return None
        return data if isinstance(data, dict) else None
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _require_current_password_proof(user: Any, data: dict[str, Any]) -> Response | None:
    """Require proof of the current login credential for password accounts.

    Load-bearing under credential authentication, not just for sessions: a
    stolen ``messages:write`` OAuth2 token must not be enough on its own to
    re-key someone's account. Enroll/Rewrap/Reset all re-derive or replace key
    material, so each of them demands the account password as a second factor
    the token itself never carries.

    Args:
        user: The requesting ``User``.
        data: The parsed JSON body, which must carry ``current_password``.

    Returns:
        A 403 response when the proof is missing or wrong, else None. Accounts
        with no usable password (OAuth-only) have no proof to give and pass.
    """
    if not user.has_usable_password():
        return None
    current_password = data.get("current_password", "")
    if not isinstance(current_password, str) or not user.check_password(current_password):
        return Response({"error": "Current password is incorrect."}, status=403)
    return None


class E2EELoginParamsView(APIView):
    """GET (anonymous): report how an identifier's account authenticates.

    Enrolled accounts get ``mode: "derived"`` plus their real Argon2id salt;
    unknown identifiers get a deterministic decoy salt so they are
    indistinguishable from enrolled accounts. Pre-enrollment accounts report
    ``mode: "legacy"`` (the raw-password form flow), which leaks their
    existence until their next login upgrades them - an accepted, shrinking
    window (the login form already reveals unverified accounts).
    """

    #: Anonymous by design - this answers a question the login form must ask
    #: *before* anyone is authenticated.
    authentication_classes: ClassVar[list] = []
    permission_classes = [AllowAny]
    #: Unthrottled, as it was as a plain Django view. Inheriting DRF's default
    #: ``AnonRateThrottle`` (60/minute, keyed per IP) would newly rate-limit
    #: the *login* flow, since every login attempt calls this first - a shared
    #: office or CGNAT egress IP would start failing to log in. Enumeration is
    #: already handled by the decoy salts rather than by rate limiting.
    throttle_classes: ClassVar[list] = []
    renderer_classes = [JSONRenderer]

    @extend_schema(exclude=True)
    def get(self, request: Request) -> Response:
        """Return the auth mode and salt for one login identifier.

        Args:
            request: The request; ``identifier`` query param holds the
                username or email typed into the login form.

        Returns:
            JSON ``{mode, auth_salt}``.
        """
        identifier = request.query_params.get("identifier", "").strip()
        if not identifier or len(identifier) > 254:
            return Response({"error": "identifier is required"}, status=400)
        return Response(login_params_for_identifier(identifier))


class E2EEEnrollView(DualAuthJsonView):
    """POST: store a freshly generated key bundle (and optionally rotate to derived auth).

    Password accounts include ``auth_key``/``auth_salt`` (plus
    ``current_password`` as proof of possession) - the server replaces the
    stored credential with the derived key, after which the raw password
    never reaches the server again. OAuth-only accounts omit all three.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=("Publishes the caller's key bundle. Requires `current_password` on accounts that have one, even when authenticating with an OAuth2 token - the token alone must never be sufficient to replace an account's key material."),
        request=e2ee_schema.E2EEEnrollRequestSerializer,
        responses={201: e2ee_schema.E2EEOkResponseSerializer, 400: None, 403: None, 409: None},
    )
    def post(self, request: Request) -> Response:
        """Create the caller's key bundle.

        Args:
            request: JSON body with ``public_key``, ``recovery_wrapped_secret``,
                optional ``password_wrapped_secret``/``password_wrap_salt``,
                optional ``auth_key``/``auth_salt``/``current_password``, and
                ``kdf_opslimit``/``kdf_memlimit``.

        Returns:
            201 JSON on success; 400 on malformed blobs; 403 on bad password
            proof; 409 when a bundle already exists.
        """
        profile = _get_profile(request)
        # profile.user is a concrete User (the permission classes guarantee an
        # authenticated request); use it for the password operations below so
        # the types stay narrow.
        user = profile.user
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)

        if MessagingKeyBundle.objects.for_profile(profile).exists():
            return Response({"error": "A key bundle already exists for this account."}, status=409)

        public_key = data.get("public_key", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        auth_key = data.get("auth_key", "")
        auth_salt = data.get("auth_salt", "")

        if not valid_blob(public_key, MAX_PUBLIC_KEY_LENGTH):
            return Response({"error": "Invalid public_key"}, status=400)
        if not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH):
            return Response({"error": "Invalid recovery_wrapped_secret"}, status=400)
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return Response({"error": "Invalid password_wrapped_secret"}, status=400)
        if not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return Response({"error": "Invalid password_wrap_salt"}, status=400)
        if bool(password_wrapped) != bool(password_wrap_salt):
            return Response({"error": "password_wrapped_secret and password_wrap_salt must be provided together"}, status=400)

        proof_error = _require_current_password_proof(user, data)
        if proof_error is not None:
            return proof_error

        rotate_auth = bool(auth_key)
        if rotate_auth:
            if not valid_blob(auth_key, MAX_SALT_LENGTH + 64) or not valid_blob(auth_salt, MAX_SALT_LENGTH):
                return Response({"error": "Invalid auth_key/auth_salt"}, status=400)
            if not user.has_usable_password():
                return Response({"error": "Current password is incorrect."}, status=403)

        try:
            kdf_opslimit = int(data.get("kdf_opslimit", 0))
            kdf_memlimit = int(data.get("kdf_memlimit", 0))
        except (TypeError, ValueError):
            return Response({"error": "Invalid kdf parameters"}, status=400)
        # A floor, not just "positive". These parameters decide how expensive it is
        # to brute-force `password_wrapped_secret` offline, and the whole point of
        # that blob is that whoever holds it - including this server - cannot open
        # it. Accepting any positive value let a caller enrol its own account with
        # Argon2 parameters weak enough to make the wrapped private key
        # recoverable from the password. Stronger-than-default is still accepted,
        # so a future client can raise them without a server change; the real
        # client always sends exactly these (frontend `e2ee-crypto.KDF_OPSLIMIT`
        # / `KDF_MEMLIMIT`, pinned to match), so nothing legitimate trips this.
        if kdf_opslimit < DEFAULT_KDF_OPSLIMIT or kdf_memlimit < DEFAULT_KDF_MEMLIMIT:
            return Response({"error": "Invalid kdf parameters"}, status=400)

        with transaction.atomic():
            try:
                bundle = MessagingKeyBundle.objects.create(
                    profile=profile,
                    public_key=public_key,
                    recovery_wrapped_secret=recovery_wrapped,
                    password_wrapped_secret=password_wrapped,
                    password_wrap_salt=password_wrap_salt,
                    kdf_opslimit=kdf_opslimit,
                    kdf_memlimit=kdf_memlimit,
                )
            except IntegrityError:
                return Response({"error": "A key bundle already exists for this account."}, status=409)
            if rotate_auth:
                AccountKdf.objects.set_auth_salt(user, auth_salt)
                user.set_password(auth_key)
                user.save(update_fields=["password"])
                # Only meaningful for a browser session, whose auth hash would
                # otherwise be invalidated by the password change and log the
                # user straight out. A credential-authenticated caller has no
                # session at all, and calling this unguarded would *create* an
                # empty one for a client that will never send the cookie back.
                if request.session.session_key:
                    update_session_auth_hash(request, user)

        logger.info("E2EE enrollment for profile %s (derived auth: %s)", profile.pk, rotate_auth)
        return Response({"version": bundle.version, "profile_slug": profile.ensure_slug()}, status=201)


class E2EEOwnKeysView(DualAuthJsonView):
    """GET: return the caller's full key bundle (wrapped blobs included)."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
    }

    @extend_schema(operation_id="e2ee_own_keys_retrieve", responses={200: e2ee_schema.E2EEOwnKeysResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's bundle, or an "enrolled: false" body when not enrolled.

        Not being enrolled yet is the common, expected state for most accounts
        (checked unconditionally on every page load to render the encryption
        status indicator), so it is reported as a normal 200 rather than a 404
        - an HTTP error status here would show up as a spurious-looking error
        in the browser console on essentially every page view for these
        accounts, even though the client handles it gracefully.

        Args:
            request: The authenticated request.

        Returns:
            JSON with every bundle field the client needs to unlock, or
            ``{"enrolled": false}`` when the account has no bundle yet.
        """
        from webauthn.helpers import bytes_to_base64url

        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        if bundle is None:
            return Response({"enrolled": False})
        # Wraps sealed to a superseded keypair are withheld (usable_for_bundle),
        # not surfaced-and-flagged: a client that unwrapped one would silently
        # adopt a dead identity. passkey_credentials lists every credential so
        # the enrollment UI can offer "use an existing passkey" vs "create one"
        # without a second endpoint - credential ids are public handles.
        wraps = [
            {"credential_id": bytes_to_base64url(bytes(wrap.credential.credential_id)), "prf_input": wrap.prf_input, "wrapped_secret": wrap.wrapped_secret} for wrap in E2EEPasskeyWrap.objects.usable_for_bundle(bundle).select_related("credential")
        ]
        wrapped_ids = {entry["credential_id"] for entry in wraps}
        credentials = [
            {
                "credential_id": bytes_to_base64url(bytes(cred.credential_id)),
                "name": cred.name,
                "is_login_factor": cred.is_login_factor,
                "has_wrap": bytes_to_base64url(bytes(cred.credential_id)) in wrapped_ids,
            }
            for cred in WebAuthnCredential.objects.filter(user=profile.user)
        ]
        return Response(
            {
                "enrolled": True,
                "public_key": bundle.public_key,
                "password_wrapped_secret": bundle.password_wrapped_secret,
                "password_wrap_salt": bundle.password_wrap_salt,
                "password_wrap_stale": bundle.password_wrap_stale,
                "recovery_wrapped_secret": bundle.recovery_wrapped_secret,
                "kdf_opslimit": bundle.kdf_opslimit,
                "kdf_memlimit": bundle.kdf_memlimit,
                "version": bundle.version,
                "profile_slug": profile.ensure_slug(),
                "passkey_wraps": wraps,
                "passkey_credentials": credentials,
            },
        )


class E2EEPartnerKeyView(DualAuthJsonView):
    """GET: return a conversation partner's public key (and nothing else)."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
    }

    @extend_schema(responses={200: e2ee_schema.E2EEPartnerKeyResponseSerializer, 404: None})
    def get(self, request: Request, profile_slug: str) -> Response:
        """Return the partner's public key when a DM relationship is permitted.

        Args:
            request: The authenticated request.
            profile_slug: The partner's profile slug.

        Returns:
            JSON ``{public_key, version}``; 404 when the partner has no bundle
            or no DM relationship is permitted in either direction.
        """
        profile = _get_profile(request)
        partner = get_object_or_404(Profile.objects.select_related("user"), slug=profile_slug)
        if partner.pk == profile.pk:
            return Response({"error": "Use the own-keys endpoint for your own bundle"}, status=400)
        if not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            return Response({"error": "Not found."}, status=404)
        bundle = MessagingKeyBundle.objects.for_profile(partner).first()
        if bundle is None:
            return Response({"error": "Not found."}, status=404)
        return Response({"public_key": bundle.public_key, "version": bundle.version})


class E2EEConversationKeyView(DualAuthJsonView):
    """GET/POST the wrapped conversation key(s) shared with one partner."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(responses={200: e2ee_schema.E2EEConversationKeysResponseSerializer, 404: None})
    def get(self, request: Request, profile_slug: str) -> Response:
        """Return the caller's wrapped copy of every key version for this pair.

        Args:
            request: The authenticated request.
            profile_slug: The partner's profile slug.

        Returns:
            JSON ``{keys: [{version, wrapped_key}], latest}`` (``latest`` is 0
            when no key exists yet).

        Raises:
            Http404: When no keys exist for the pair and no DM relationship is
                permitted in either direction - identical to an unknown slug,
                so this endpoint can't be used to probe which profile slugs
                exist. Existing keys are always returned regardless of the
                current relationship: a participant must stay able to decrypt
                their history even after a block or privacy change.
        """
        profile = _get_profile(request)
        partner = get_object_or_404(Profile, slug=profile_slug)
        if partner.pk == profile.pk:
            return Response({"error": "No self-conversations"}, status=400)
        rows = list(ConversationKey.objects.between(profile, partner))
        if not rows and not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            raise Http404
        keys = [{"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)} for row in rows]
        return Response({"keys": keys, "latest": rows[-1].version if rows else 0})

    @extend_schema(request=e2ee_schema.E2EEConversationKeyCreateRequestSerializer, responses={200: e2ee_schema.E2EEWrappedKeySerializer, 201: e2ee_schema.E2EEWrappedKeySerializer, 400: None, 404: None})
    def post(self, request: Request, profile_slug: str) -> Response:
        """Store the next conversation-key version for this pair.

        The creating client generates the random key and seals it to both
        participants' public keys; the server stores the two blobs it cannot
        open. A concurrent-create race is resolved by returning the winner.

        Args:
            request: JSON body with ``version``, ``wrapped_for_me``,
                ``wrapped_for_partner`` (the server maps them onto the
                canonical low/high pair ordering).
            profile_slug: The partner's profile slug.

        Returns:
            201 with the caller's wrapped copy on success; 200 with the
            existing winner's copy when racing; 400/403/409 on invalid input.
        """
        profile = _get_profile(request)
        partner = get_object_or_404(Profile, slug=profile_slug)
        if partner.pk == profile.pk:
            return Response({"error": "No self-conversations"}, status=400)
        if not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            return Response({"error": "Not found."}, status=404)
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)

        wrapped_for_me = data.get("wrapped_for_me", "")
        wrapped_for_partner = data.get("wrapped_for_partner", "")
        if not valid_blob(wrapped_for_me, MAX_WRAPPED_CONVERSATION_KEY_LENGTH) or not valid_blob(wrapped_for_partner, MAX_WRAPPED_CONVERSATION_KEY_LENGTH):
            return Response({"error": "Invalid wrapped key blobs"}, status=400)
        wrapped_for_low, wrapped_for_high = (wrapped_for_me, wrapped_for_partner) if profile.pk < partner.pk else (wrapped_for_partner, wrapped_for_me)
        low, high = ConversationKey.canonical_pair(profile, partner)

        if not MessagingKeyBundle.objects.for_profile(profile).exists() or not MessagingKeyBundle.objects.for_profile(partner).exists():
            return Response({"error": "Both participants must be enrolled."}, status=409)

        latest = ConversationKey.objects.between(profile, partner).order_by("-version").first()
        expected_version = (latest.version if latest else 0) + 1
        try:
            requested_version = int(data.get("version", 0))
        except (TypeError, ValueError):
            return Response({"error": "Invalid version"}, status=400)
        if requested_version != expected_version:
            return Response({"error": f"Expected version {expected_version}.", "expected": expected_version}, status=409)

        try:
            with transaction.atomic():
                row = ConversationKey.objects.create(
                    profile_low=low,
                    profile_high=high,
                    wrapped_for_low=wrapped_for_low,
                    wrapped_for_high=wrapped_for_high,
                    version=requested_version,
                    created_by=profile,
                )
        except IntegrityError:
            # Lost the race - the concurrent creator's key is canonical.
            row = get_object_or_404(ConversationKey, profile_low=low, profile_high=high, version=requested_version)
            return Response({"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)}, status=200)
        return Response({"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)}, status=201)


class E2EERewrapView(DualAuthJsonView):
    """POST: replace wrapped private-key copies (same key, new wrapping).

    Used after a password reset (re-wrap under the new password, clearing the
    stale flag) and when regenerating the recovery key. The private key itself
    never changes here - only which secrets can unwrap it.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=(
            "Re-wraps the caller's private key under new secrets. Any rewrap - password, recovery, or both - "
            "requires `current_password` on password-backed accounts, even for OAuth2 callers; see the enroll "
            "endpoint for the rationale. Accounts with no usable password (OAuth-only) have no proof to give."
        ),
    )
    @extend_schema(request=e2ee_schema.E2EERewrapRequestSerializer, responses={200: e2ee_schema.E2EEOkResponseSerializer, 400: None})
    def post(self, request: Request) -> Response:
        """Update wrapped copies on the caller's bundle.

        Args:
            request: JSON body with optional ``password_wrapped_secret`` +
                ``password_wrap_salt`` (together) and/or
                ``recovery_wrapped_secret``.

        Returns:
            JSON ``{ok: true}``; 400 on malformed blobs; 404 when not enrolled.
        """
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        if bundle is None:
            return Response({"error": "Not enrolled."}, status=404)
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)

        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return Response({"error": "Invalid password_wrapped_secret"}, status=400)
        if not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return Response({"error": "Invalid password_wrap_salt"}, status=400)
        if not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return Response({"error": "Invalid recovery_wrapped_secret"}, status=400)
        if bool(password_wrapped) != bool(password_wrap_salt):
            return Response({"error": "password_wrapped_secret and password_wrap_salt must be provided together"}, status=400)
        if not password_wrapped and not recovery_wrapped:
            return Response({"error": "Nothing to update"}, status=400)
        # Proof is required for *either* wrapped copy, not just the password
        # one. Gating it on `password_wrapped` alone left the recovery-only
        # rewrap unauthenticated beyond the bearer token: a stolen
        # `messages:write` credential could post a `recovery_wrapped_secret`
        # by itself and overwrite the victim's recovery-wrapped private key
        # without knowing their password. The private key is unrecoverable
        # once its last valid wrapping is replaced, so that is a silent,
        # permanent destruction of the account's messages - the exact outcome
        # the password proof on the other branch exists to prevent, reachable
        # by simply omitting a field.
        # Unconditional: the guard above already established that at least one
        # wrapped copy is being replaced. (OAuth-only accounts have no password
        # to prove and pass through - see the helper.)
        proof_error = _require_current_password_proof(profile.user, data)
        if proof_error is not None:
            return proof_error

        update_fields = ["updated"]
        if password_wrapped:
            bundle.password_wrapped_secret = password_wrapped
            bundle.password_wrap_salt = password_wrap_salt
            bundle.password_wrap_stale = False
            update_fields += ["password_wrapped_secret", "password_wrap_salt", "password_wrap_stale"]
        if recovery_wrapped:
            bundle.recovery_wrapped_secret = recovery_wrapped
            update_fields.append("recovery_wrapped_secret")
        bundle.save(update_fields=update_fields)
        return Response({"ok": True})


class E2EEPasskeyWrapView(DualAuthJsonView):
    """POST/DELETE a passkey-wrapped copy of the caller's private key.

    The blob was wrapped client-side under an HKDF of the WebAuthn ``prf``
    output for one of the caller's own passkeys; the server stores what it
    cannot open, exactly like the password and recovery wraps. POST upserts
    (re-enrolling a credential replaces its wrap); DELETE removes one wrap
    without touching the passkey itself (deleting the passkey cascades).

    Both methods demand the account-password proof on password-backed
    accounts, same rationale as ``E2EERewrapView``: adding an unlock path -
    or destroying one, which for an account whose recovery key was never
    saved is a data-loss lever - must cost more than a bearer token.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    def _resolve_credential(self, request: Request, credential_id_b64: str) -> WebAuthnCredential | None:
        """Resolve a base64url credential id to one of the caller's passkeys.

        Args:
            request: The authenticated request.
            credential_id_b64: The credential's rawId, base64url-encoded.

        Returns:
            The credential, or None when malformed or not the caller's own -
            indistinguishable on purpose, so this cannot probe which
            credential ids exist.
        """
        from webauthn.helpers import base64url_to_bytes

        try:
            raw_id = base64url_to_bytes(credential_id_b64)
        except (ValueError, TypeError):
            return None
        return WebAuthnCredential.objects.filter(user=request.user, credential_id=raw_id).first()

    @extend_schema(
        description=(
            "Stores (or replaces) a passkey-wrapped copy of the caller's E2EE private key. The wrap was "
            "computed client-side from the WebAuthn prf extension's output; the server never sees the PRF "
            "secret. Requires `current_password` on password-backed accounts, even for OAuth2 callers - "
            "see the enroll endpoint for the rationale."
        ),
    )
    def post(self, request: Request) -> Response:
        """Create or replace the wrap for one of the caller's passkeys.

        Args:
            request: JSON body with ``credential_id`` (base64url), ``prf_input``
                (base64 32-byte PRF evaluation input), ``wrapped_secret``, and
                ``current_password`` on password-backed accounts.

        Returns:
            201 JSON ``{ok: true}`` on create, 200 on replace; 400 on malformed
            input or an unknown credential; 403 on bad proof; 404 when not
            enrolled.
        """
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        if bundle is None:
            return Response({"error": "Not enrolled."}, status=404)
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)

        prf_input = data.get("prf_input", "")
        wrapped_secret = data.get("wrapped_secret", "")
        if not valid_blob(prf_input, MAX_SALT_LENGTH):
            return Response({"error": "Invalid prf_input"}, status=400)
        if not valid_blob(wrapped_secret, MAX_WRAPPED_SECRET_LENGTH):
            return Response({"error": "Invalid wrapped_secret"}, status=400)

        credential_id = data.get("credential_id", "")
        credential = self._resolve_credential(request, credential_id) if isinstance(credential_id, str) else None
        if credential is None:
            return Response({"error": "Unknown credential"}, status=400)

        proof_error = _require_current_password_proof(profile.user, data)
        if proof_error is not None:
            return proof_error

        _, created = E2EEPasskeyWrap.objects.update_or_create(
            credential=credential,
            defaults={"bundle": bundle, "prf_input": prf_input, "wrapped_secret": wrapped_secret, "bundle_version": bundle.version},
        )
        logger.info("E2EE passkey wrap %s for profile %s (credential %s)", "created" if created else "replaced", profile.pk, credential.pk)
        return Response({"ok": True}, status=201 if created else 200)

    @extend_schema(
        description=(
            "Removes the wrap for one passkey (the passkey itself is untouched). Requires `current_password` "
            "on password-backed accounts: for an account whose recovery key was never saved, destroying an "
            "unlock path is a data-loss lever, and a bearer token alone must not reach it."
        ),
    )
    def delete(self, request: Request, credential_id: str) -> Response:
        """Delete the wrap for one of the caller's passkeys.

        Args:
            request: The authenticated request; JSON body may carry
                ``current_password``.
            credential_id: The credential's rawId, base64url-encoded.

        Returns:
            JSON ``{ok: true}``; 403 on bad proof; 404 for an unknown
            credential or one with no wrap.
        """
        profile = _get_profile(request)
        credential = self._resolve_credential(request, credential_id)
        if credential is None:
            return Response({"error": "Not found."}, status=404)
        data = _json_body(request) or {}
        proof_error = _require_current_password_proof(profile.user, data)
        if proof_error is not None:
            return proof_error
        deleted, _ = E2EEPasskeyWrap.objects.filter(credential=credential, bundle__profile=profile).delete()
        if not deleted:
            return Response({"error": "Not found."}, status=404)
        return Response({"ok": True})


class E2EEGroupKeyView(DualAuthJsonView):
    """GET/POST the wrapped group-key versions for one group chat.

    GET returns only the caller's own envelopes (one per version they were a
    member for), plus what a client needs to rotate: the latest version
    number, whether that version still covers the group's current membership,
    and - when every member is enrolled - each member's public key.

    POST stores the next version: the creating client generates the random
    key and seals it once per active member; the server verifies the envelope
    set covers the active membership exactly and stores blobs it cannot open.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    def _resolve(self, request: Request, group_uuid: UUID) -> tuple[Profile, Any, Any] | None:
        """Resolve the caller's profile, the group, and their active membership.

        Args:
            request: The authenticated request.
            group_uuid: UUID of the group chat.

        Returns:
            ``(profile, group, membership)`` or None when the caller isn't an
            active member (indistinguishable from a nonexistent group).
        """
        from urbanlens.dashboard.models.group_chats.model import GroupChat

        profile = _get_profile(request)
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None:
            return None
        membership = group.membership_for(profile)
        if membership is None:
            return None
        return profile, group, membership

    @extend_schema(
        description=(
            "Returns the caller's own group-key envelopes plus the rotation state.\n\n"
            "`members[].id` is an **opaque per-(group, member) token**, never a profile slug - it exists "
            "precisely so this payload cannot reveal the identity of members whose profile visibility masks "
            "them. Clients MUST round-trip these `id` values verbatim as the keys of the `wrapped` object "
            "they POST back. A payload keyed by profile slugs (or by anything else) will not match the "
            "server's recomputed token set and is rejected with 409; refetch this endpoint and retry."
        ),
    )
    @extend_schema(responses={200: e2ee_schema.E2EEGroupKeysResponseSerializer, 404: None})
    def get(self, request: Request, group_uuid: UUID) -> Response:
        """Return the caller's envelopes and the group's rotation state.

        Args:
            request: The authenticated request.
            group_uuid: UUID of the group chat.

        Returns:
            JSON ``{keys, latest, needs_rotation, members}`` - ``members`` is
            a ``[{id, public_key}]`` list when every active member is enrolled
            (so the caller can rotate), else null. ``id`` is an opaque
            per-(group, member) token (see ``services.security.e2ee.group_member_token``)
            - never a slug, which would hand every member the real identity of
            members whose ``profile_visibility`` masks them elsewhere. 404 for
            non-members and unknown groups.
        """
        from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope, MessagingKeyBundle
        from urbanlens.dashboard.services.security.e2ee import group_member_token

        resolved = self._resolve(request, group_uuid)
        if resolved is None:
            return Response({"error": "Not found."}, status=404)
        profile, group, _membership = resolved

        key_rows = list(GroupKey.objects.for_group(group).order_by("version"))
        own_envelopes = {envelope.key_id: envelope.wrapped_key for envelope in GroupKeyEnvelope.objects.filter(key__group=group, profile=profile)}
        keys = [{"version": row.version, "wrapped_key": own_envelopes[row.pk]} for row in key_rows if row.pk in own_envelopes]
        latest = key_rows[-1].version if key_rows else 0

        member_profiles = [membership.profile for membership in group.active_memberships().select_related("profile", "profile__user")]
        bundles = {bundle.profile_id: bundle for bundle in MessagingKeyBundle.objects.for_profiles(member_profiles)}
        all_enrolled = all(member.pk in bundles for member in member_profiles)

        needs_rotation = latest == 0
        if not needs_rotation:
            latest_member_ids = set(GroupKeyEnvelope.objects.filter(key=key_rows[-1]).values_list("profile_id", flat=True))
            needs_rotation = latest_member_ids != {member.pk for member in member_profiles}

        members = None
        if all_enrolled:
            members = [{"id": group_member_token(group.uuid, member.pk), "public_key": bundles[member.pk].public_key} for member in member_profiles]
        return Response({"keys": keys, "latest": latest, "needs_rotation": needs_rotation, "members": members})

    @extend_schema(
        description=(
            "Stores the next group-key version.\n\n"
            "`wrapped` must be keyed by the opaque `members[].id` tokens returned by GET on this same "
            "endpoint, round-tripped verbatim, and must cover the group's current active membership exactly. "
            "Any other key set - notably profile slugs - is rejected with 409."
        ),
    )
    @extend_schema(request=e2ee_schema.E2EEGroupKeyCreateRequestSerializer, responses={200: e2ee_schema.E2EEWrappedKeySerializer, 201: e2ee_schema.E2EEWrappedKeySerializer, 400: None, 404: None})
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Store the next group-key version.

        Args:
            request: JSON body with ``version`` and ``wrapped`` (a mapping of
                each member's opaque rotation token - the ``id`` the GET
                response issued - to that member's sealed blob; must cover the
                active membership exactly).
            group_uuid: UUID of the group chat.

        Returns:
            201 with the caller's envelope on success; 200 with the existing
            winner's envelope when racing; 400/404/409 on invalid input.
        """
        from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope, MessagingKeyBundle
        from urbanlens.dashboard.services.security.e2ee import group_member_token

        resolved = self._resolve(request, group_uuid)
        if resolved is None:
            return Response({"error": "Not found."}, status=404)
        profile, group, _membership = resolved
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)

        wrapped = data.get("wrapped")
        if not isinstance(wrapped, dict) or not wrapped:
            return Response({"error": "Invalid wrapped envelopes"}, status=400)

        # Keyed by opaque per-(group, member) tokens, recomputed here rather
        # than decoded - the client just round-trips the ids the GET response
        # issued. A stale client still keying by slug gets the same 409 as any
        # other membership mismatch and retries after refetching.
        members_by_token = {group_member_token(group.uuid, membership.profile_id): membership.profile for membership in group.active_memberships().select_related("profile", "profile__user")}
        if set(wrapped) != set(members_by_token):
            return Response({"error": "Envelopes must cover the group's current members exactly."}, status=409)
        for blob in wrapped.values():
            if not valid_blob(blob, MAX_WRAPPED_CONVERSATION_KEY_LENGTH):
                return Response({"error": "Invalid wrapped envelopes"}, status=400)
        enrolled_count = MessagingKeyBundle.objects.for_profiles(members_by_token.values()).count()
        if enrolled_count != len(members_by_token):
            return Response({"error": "Every member must be enrolled before the group can encrypt."}, status=409)

        latest = GroupKey.objects.for_group(group).order_by("-version").first()
        expected_version = (latest.version if latest else 0) + 1
        try:
            requested_version = int(data.get("version", 0))
        except (TypeError, ValueError):
            return Response({"error": "Invalid version"}, status=400)
        if requested_version != expected_version:
            return Response({"error": f"Expected version {expected_version}.", "expected": expected_version}, status=409)

        try:
            with transaction.atomic():
                key_row = GroupKey.objects.create(group=group, version=requested_version, created_by=profile)
                GroupKeyEnvelope.objects.bulk_create(
                    [GroupKeyEnvelope(key=key_row, profile=member, wrapped_key=wrapped[token]) for token, member in members_by_token.items()],
                )
        except IntegrityError:
            # Lost the race - the concurrent creator's key is canonical.
            winner = get_object_or_404(GroupKey, group=group, version=requested_version)
            envelope = GroupKeyEnvelope.objects.filter(key=winner, profile=profile).first()
            if envelope is None:
                return Response({"error": "No envelope for this member."}, status=409)
            return Response({"version": winner.version, "wrapped_key": envelope.wrapped_key}, status=200)
        return Response({"version": key_row.version, "wrapped_key": wrapped[group_member_token(group.uuid, profile.pk)]}, status=201)


class E2EEChangePasswordView(LoginRequiredMixin, View):
    """POST: change (or, for OAuth accounts, set) the login password.

    Always moves the account to derived auth: the client derives the new
    credential (``new_auth_key``) and a fresh salt in the browser, so the raw
    new password never reaches the server. Accounts that already have a
    password must prove possession of the current one (``current_secret`` -
    the raw password for legacy accounts, the derived authKey for derived
    accounts; either way it's what ``check_password`` matches). OAuth-only
    accounts with no usable password set one without a current secret.

    When the device holds the decrypted private key, the client re-wraps it
    under the new password and sends ``password_wrapped_secret``/
    ``password_wrap_salt`` along; otherwise any existing password-wrapped
    copy is flagged stale (the old password is gone).

    Note:
        **Deliberately NOT converted to a dual-auth endpoint**, unlike every
        other view in this module. This is the one endpoint here that calls
        ``user.set_password()`` against the account's *login* credential, so
        exposing it to credential authentication would let a scoped messaging
        token (``messages:write`` - granted for the ability to send chat
        messages) rotate the password of the account that issued it. That is
        account takeover, and it converts the compromise of a single narrow,
        revocable token into the permanent loss of the whole account. Changing
        a login password is a first-party, session-and-CSRF-protected action
        that must require the browser's own authenticated session; a mobile
        client needing this should send the user through the web flow. Keep
        this a ``LoginRequiredMixin``/``View``: it is session-only by design,
        not by omission.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Rotate the account's password and reconcile its E2EE state.

        Args:
            request: JSON body with ``current_secret`` (required when the
                account has a usable password), ``new_auth_key``,
                ``new_auth_salt``, and optional ``password_wrapped_secret``
                + ``password_wrap_salt``.

        Returns:
            JSON ``{ok: true, had_password}``; 400 on malformed input; 403 on
            a wrong current secret.
        """
        profile = _get_profile(request)
        user = profile.user
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")

        new_auth_key = data.get("new_auth_key", "")
        new_auth_salt = data.get("new_auth_salt", "")
        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        if not valid_blob(new_auth_key, MAX_SALT_LENGTH + 64) or not valid_blob(new_auth_salt, MAX_SALT_LENGTH):
            return HttpResponseBadRequest("Invalid new credential")
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrapped_secret")
        if not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrap_salt")
        if bool(password_wrapped) != bool(password_wrap_salt):
            return HttpResponseBadRequest("password_wrapped_secret and password_wrap_salt must be provided together")

        had_password = user.has_usable_password()
        if had_password:
            current_secret = data.get("current_secret", "")
            if not isinstance(current_secret, str) or not user.check_password(current_secret):
                return JsonResponse({"error": "Your current password is incorrect."}, status=403)

        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        with transaction.atomic():
            AccountKdf.objects.set_auth_salt(user, new_auth_salt)
            user.set_password(new_auth_key)
            user.save(update_fields=["password"])
            update_session_auth_hash(request, user)
            if bundle is not None:
                if password_wrapped:
                    bundle.password_wrapped_secret = password_wrapped
                    bundle.password_wrap_salt = password_wrap_salt
                    bundle.password_wrap_stale = False
                    bundle.save(update_fields=["password_wrapped_secret", "password_wrap_salt", "password_wrap_stale", "updated"])
                elif bundle.password_wrapped_secret:
                    # The old wrap can't be opened with the new password and
                    # this device couldn't produce a fresh one (key locked).
                    bundle.password_wrap_stale = True
                    bundle.save(update_fields=["password_wrap_stale", "updated"])

        logger.info("Password %s for user %s (derived auth)", "changed" if had_password else "set", user.pk)
        return JsonResponse({"ok": True, "had_password": had_password})


class E2EERewrapAllView(DualAuthJsonView):
    """GET: every wrapped key copy addressed to the caller, for bulk re-wrap.

    Used by the reset flow when the client still holds (or can unlock) the
    OLD private key: it unseals each copy locally, re-seals it to the new
    public key, and submits the results alongside the reset so the caller's
    message history stays readable. Returns only blobs the caller could
    already fetch one conversation/group at a time - this just avoids N
    round trips.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
    }

    @extend_schema(responses={200: e2ee_schema.E2EERewrapAllResponseSerializer})
    def get(self, request: Request) -> Response:
        """List the caller's sealed conversation-key copies and group envelopes.

        Args:
            request: The authenticated request.

        Returns:
            JSON ``{conversation_keys: [{id, wrapped_key}],
            group_envelopes: [{id, wrapped_key}]}``; 404 when not enrolled.
        """
        from django.db.models import Q

        from urbanlens.dashboard.models.e2ee import GroupKeyEnvelope

        profile = _get_profile(request)
        if not MessagingKeyBundle.objects.for_profile(profile).exists():
            return Response({"error": "Not enrolled."}, status=404)

        conversation_keys = [{"id": row.pk, "wrapped_key": row.wrapped_for(profile.pk)} for row in ConversationKey.objects.filter(Q(profile_low=profile) | Q(profile_high=profile))]
        group_envelopes = [{"id": envelope.pk, "wrapped_key": envelope.wrapped_key} for envelope in GroupKeyEnvelope.objects.filter(profile=profile)]
        return Response({"conversation_keys": conversation_keys, "group_envelopes": group_envelopes})


#: Upper bound on rewrapped-entry lists accepted by the reset endpoint. Far
#: above any plausible real count (one entry per conversation-key version /
#: group membership) - purely an abuse guard against giant request bodies.
MAX_REWRAP_ENTRIES = 10_000


def _parse_rewrap_entries(raw: Any) -> dict[int, str] | None:
    """Validate a client-submitted rewrapped-key list into an id→blob mapping.

    Args:
        raw: The JSON value (expected: list of ``{id, wrapped_key}`` dicts).

    Returns:
        Mapping of row id to the re-sealed blob, or None when the shape or
        any blob is invalid.
    """
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > MAX_REWRAP_ENTRIES:
        return None
    entries: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        row_id = item.get("id")
        wrapped = item.get("wrapped_key")
        if not isinstance(row_id, int) or not isinstance(wrapped, str) or not valid_blob(wrapped, MAX_WRAPPED_CONVERSATION_KEY_LENGTH):
            return None
        entries[row_id] = wrapped
    return entries


class E2EEResetView(DualAuthJsonView):
    """POST: replace the caller's keypair entirely (last resort).

    When the client still holds the old private key it submits re-sealed
    copies of every conversation/group key alongside the reset, and the
    caller's message history stays readable under the new keypair. Without
    them, old encrypted messages become permanently unreadable to the caller
    (conversation partners keep their own copies - old ``ConversationKey``
    versions are retained for them). Requires a typed confirmation string.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=(
            'Replaces the caller\'s keypair. Requires both the literal `confirm: "RESET"` string and, on '
            "accounts that have a password, `current_password` - a `messages:write` token alone must never "
            "be able to re-key an account and lock its owner out of their own history."
        ),
    )
    @extend_schema(request=e2ee_schema.E2EEResetRequestSerializer, responses={200: e2ee_schema.E2EEResetResponseSerializer, 400: None, 403: None})
    def post(self, request: Request) -> Response:
        """Replace the caller's key bundle with brand-new key material.

        Args:
            request: JSON body with ``confirm`` (must equal ``"RESET"``),
                ``public_key``, ``recovery_wrapped_secret``, optional
                ``password_wrapped_secret``/``password_wrap_salt``, and
                optional ``rewrapped_conversation_keys``/
                ``rewrapped_group_envelopes`` (lists of ``{id, wrapped_key}``
                re-sealed to the NEW public key; ids must be the caller's own
                rows). The bundle swap and every rewrap apply in one atomic
                transaction - there is no partial state.

        Returns:
            JSON ``{version, rewrapped}``; 400 on malformed input, missing
            confirmation, or a rewrap id that isn't the caller's; 404 when
            not enrolled.
        """
        from django.db.models import Q

        from urbanlens.dashboard.models.e2ee import GroupKeyEnvelope

        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        if bundle is None:
            return Response({"error": "Not enrolled."}, status=404)
        data = _json_body(request)
        if data is None:
            return Response({"error": MALFORMED_JSON_BODY_MESSAGE}, status=400)
        if data.get("confirm") != RESET_CONFIRMATION:
            return Response({"error": "Missing confirmation"}, status=400)
        proof_error = _require_current_password_proof(profile.user, data)
        if proof_error is not None:
            return proof_error

        public_key = data.get("public_key", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        if not valid_blob(public_key, MAX_PUBLIC_KEY_LENGTH) or not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH):
            return Response({"error": "Invalid key material"}, status=400)
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False) or not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return Response({"error": "Invalid key material"}, status=400)
        if bool(password_wrapped) != bool(password_wrap_salt):
            return Response({"error": "password_wrapped_secret and password_wrap_salt must be provided together"}, status=400)

        rewrapped_conversations = _parse_rewrap_entries(data.get("rewrapped_conversation_keys"))
        rewrapped_envelopes = _parse_rewrap_entries(data.get("rewrapped_group_envelopes"))
        if rewrapped_conversations is None or rewrapped_envelopes is None:
            return Response({"error": "Invalid rewrapped key entries"}, status=400)

        # Resolve every submitted id to a row the caller actually owns BEFORE
        # writing anything - a single foreign/unknown id rejects the whole
        # request rather than partially applying it.
        conversation_rows = []
        if rewrapped_conversations:
            conversation_rows = list(
                ConversationKey.objects.filter(Q(profile_low=profile) | Q(profile_high=profile), pk__in=rewrapped_conversations),
            )
            if len(conversation_rows) != len(rewrapped_conversations):
                return Response({"error": "Unknown conversation key id"}, status=400)
        envelope_rows = []
        if rewrapped_envelopes:
            envelope_rows = list(GroupKeyEnvelope.objects.filter(profile=profile, pk__in=rewrapped_envelopes))
            if len(envelope_rows) != len(rewrapped_envelopes):
                return Response({"error": "Unknown group envelope id"}, status=400)

        with transaction.atomic():
            # The bundle was read before any of the rewrapping above, and the client
            # computed every rewrap against *that* key. If a second reset landed in the
            # meantime (a double-submitted or retried request is the realistic way),
            # applying these rewraps now would seal some conversations to the superseded
            # key while the bundle advertises the newer one - i.e. permanently
            # undecryptable threads. Re-read under a row lock and refuse if it moved;
            # the client can restart the reset against the current key.
            locked_bundle = MessagingKeyBundle.objects.select_for_update().filter(pk=bundle.pk).first()
            if locked_bundle is None or locked_bundle.version != bundle.version:
                return Response({"error": "Your key bundle changed while this reset was in progress. Please try again."}, status=409)
            bundle = locked_bundle

            # Only ever the caller's own side of each pair - the partner's
            # sealed copy is untouchable from this endpoint by construction.
            for row in conversation_rows:
                if row.profile_low_id == profile.pk:
                    row.wrapped_for_low = rewrapped_conversations[row.pk]
                    row.save(update_fields=["wrapped_for_low", "updated"])
                else:
                    row.wrapped_for_high = rewrapped_conversations[row.pk]
                    row.save(update_fields=["wrapped_for_high", "updated"])
            for envelope in envelope_rows:
                envelope.wrapped_key = rewrapped_envelopes[envelope.pk]
                envelope.save(update_fields=["wrapped_key", "updated"])

            # Passkey wraps encrypt the OLD private key - useless and
            # misleading once the keypair rotates, so they die in the same
            # transaction. The bundle_version stamp on each wrap is the
            # backstop if one ever survives; clients re-enroll wraps from any
            # device that unlocks under the new key.
            E2EEPasskeyWrap.objects.filter(bundle=bundle).delete()

            bundle.public_key = public_key
            bundle.recovery_wrapped_secret = recovery_wrapped
            bundle.password_wrapped_secret = password_wrapped
            bundle.password_wrap_salt = password_wrap_salt
            bundle.password_wrap_stale = False
            bundle.version += 1
            bundle.save(
                update_fields=[
                    "public_key",
                    "recovery_wrapped_secret",
                    "password_wrapped_secret",
                    "password_wrap_salt",
                    "password_wrap_stale",
                    "version",
                    "updated",
                ],
            )

        rewrapped_count = len(conversation_rows) + len(envelope_rows)
        # Counted after the swap, so it describes the state the caller is now in.
        # A submitted payload only has to name rows the caller owns - it does not
        # have to name all of them, which is correct (a user who lost their key
        # cannot re-seal anything and resets to get a working account back). But
        # the rows it left out are now sealed to a key that no longer exists,
        # i.e. permanently unreadable, and the caller is the only one who can
        # tell the user that. Reporting it is the difference between "your
        # history is gone" and finding out months later.
        owned_conversations = ConversationKey.objects.filter(Q(profile_low=profile) | Q(profile_high=profile)).count()
        owned_envelopes = GroupKeyEnvelope.objects.filter(profile=profile).count()
        not_rewrapped = (owned_conversations + owned_envelopes) - rewrapped_count
        logger.info(
            "E2EE key reset for profile %s (now v%s, %s key copies re-wrapped, %s left sealed to the retired key)",
            profile.pk,
            bundle.version,
            rewrapped_count,
            not_rewrapped,
        )
        return Response({"version": bundle.version, "rewrapped": rewrapped_count, "not_rewrapped": not_rewrapped})
