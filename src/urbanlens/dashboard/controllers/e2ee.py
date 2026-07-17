"""Endpoints for direct-message end-to-end encryption key storage.

Every blob accepted here was encrypted client-side; these views only validate
shape, enforce ownership, and store. See ``docs/e2ee.md`` for the scheme and
``services/e2ee.py`` for the shared helpers.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.account.model import AccountKdf
from urbanlens.dashboard.models.e2ee import ConversationKey, MessagingKeyBundle
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.direct_messages import can_direct_message
from urbanlens.dashboard.services.e2ee import (
    MAX_PUBLIC_KEY_LENGTH,
    MAX_SALT_LENGTH,
    MAX_WRAPPED_CONVERSATION_KEY_LENGTH,
    MAX_WRAPPED_SECRET_LENGTH,
    login_params_for_identifier,
    valid_blob,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

#: Confirmation string the reset endpoint requires, to make the destructive
#: consequence (old encrypted messages become unreadable) an explicit choice.
RESET_CONFIRMATION = "RESET"


def _get_profile(request: HttpRequest) -> Profile:
    """Return (creating if needed) the requesting user's profile.

    Args:
        request: The authenticated request.

    Returns:
        The user's Profile.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _json_body(request: HttpRequest) -> dict[str, Any] | None:
    """Parse the request body as a JSON object.

    Args:
        request: The incoming request.

    Returns:
        The parsed dict, or None when the body is not a JSON object.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


class E2EELoginParamsView(View):
    """GET (anonymous): report how an identifier's account authenticates.

    Enrolled accounts get ``mode: "derived"`` plus their real Argon2id salt;
    unknown identifiers get a deterministic decoy salt so they are
    indistinguishable from enrolled accounts. Pre-enrollment accounts report
    ``mode: "legacy"`` (the raw-password form flow), which leaks their
    existence until their next login upgrades them - an accepted, shrinking
    window (the login form already reveals unverified accounts).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return the auth mode and salt for one login identifier.

        Args:
            request: The request; ``identifier`` query param holds the
                username or email typed into the login form.

        Returns:
            JSON ``{mode, auth_salt}``.
        """
        identifier = request.GET.get("identifier", "").strip()
        if not identifier or len(identifier) > 254:
            return HttpResponseBadRequest("identifier is required")
        return JsonResponse(login_params_for_identifier(identifier))


class E2EEEnrollView(LoginRequiredMixin, View):
    """POST: store a freshly generated key bundle (and optionally rotate to derived auth).

    Password accounts include ``auth_key``/``auth_salt`` (plus
    ``current_password`` as proof of possession) - the server replaces the
    stored credential with the derived key, after which the raw password
    never reaches the server again. OAuth-only accounts omit all three.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
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
        # profile.user is a concrete User (LoginRequiredMixin guarantees an
        # authenticated request); use it for the password operations below so
        # the types stay narrow.
        user = profile.user
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")

        if MessagingKeyBundle.objects.filter(profile=profile).exists():
            return JsonResponse({"error": "A key bundle already exists for this account."}, status=409)

        public_key = data.get("public_key", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        auth_key = data.get("auth_key", "")
        auth_salt = data.get("auth_salt", "")

        if not valid_blob(public_key, MAX_PUBLIC_KEY_LENGTH):
            return HttpResponseBadRequest("Invalid public_key")
        if not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH):
            return HttpResponseBadRequest("Invalid recovery_wrapped_secret")
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrapped_secret")
        if not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrap_salt")
        if bool(password_wrapped) != bool(password_wrap_salt):
            return HttpResponseBadRequest("password_wrapped_secret and password_wrap_salt must be provided together")

        rotate_auth = bool(auth_key)
        if rotate_auth:
            if not valid_blob(auth_key, MAX_SALT_LENGTH + 64) or not valid_blob(auth_salt, MAX_SALT_LENGTH):
                return HttpResponseBadRequest("Invalid auth_key/auth_salt")
            # Rotating the login credential requires proof the caller knows the
            # current password - a hijacked session must not be able to lock
            # the real owner out.
            current_password = data.get("current_password", "")
            if not user.has_usable_password() or not user.check_password(current_password):
                return JsonResponse({"error": "Current password is incorrect."}, status=403)

        try:
            kdf_opslimit = int(data.get("kdf_opslimit", 0))
            kdf_memlimit = int(data.get("kdf_memlimit", 0))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid kdf parameters")
        if kdf_opslimit <= 0 or kdf_memlimit <= 0:
            return HttpResponseBadRequest("Invalid kdf parameters")

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
                return JsonResponse({"error": "A key bundle already exists for this account."}, status=409)
            if rotate_auth:
                AccountKdf.objects.set_auth_salt(user, auth_salt)
                user.set_password(auth_key)
                user.save(update_fields=["password"])
                update_session_auth_hash(request, user)

        logger.info("E2EE enrollment for profile %s (derived auth: %s)", profile.pk, rotate_auth)
        return JsonResponse({"version": bundle.version, "profile_slug": profile.ensure_slug()}, status=201)


class E2EEOwnKeysView(LoginRequiredMixin, View):
    """GET: return the caller's full key bundle (wrapped blobs included)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return the caller's bundle, or 404 when not enrolled.

        Args:
            request: The authenticated request.

        Returns:
            JSON with every bundle field the client needs to unlock.
        """
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.filter(profile=profile).first()
        if bundle is None:
            return JsonResponse({"error": "Not enrolled."}, status=404)
        return JsonResponse(
            {
                "public_key": bundle.public_key,
                "password_wrapped_secret": bundle.password_wrapped_secret,
                "password_wrap_salt": bundle.password_wrap_salt,
                "password_wrap_stale": bundle.password_wrap_stale,
                "recovery_wrapped_secret": bundle.recovery_wrapped_secret,
                "kdf_opslimit": bundle.kdf_opslimit,
                "kdf_memlimit": bundle.kdf_memlimit,
                "version": bundle.version,
                "profile_slug": profile.ensure_slug(),
            },
        )


class E2EEPartnerKeyView(LoginRequiredMixin, View):
    """GET: return a conversation partner's public key (and nothing else)."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
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
            return HttpResponseBadRequest("Use the own-keys endpoint for your own bundle")
        if not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            return JsonResponse({"error": "Not found."}, status=404)
        bundle = MessagingKeyBundle.objects.filter(profile=partner).first()
        if bundle is None:
            return JsonResponse({"error": "Not found."}, status=404)
        return JsonResponse({"public_key": bundle.public_key, "version": bundle.version})


class E2EEConversationKeyView(LoginRequiredMixin, View):
    """GET/POST the wrapped conversation key(s) shared with one partner."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Return the caller's wrapped copy of every key version for this pair.

        Args:
            request: The authenticated request.
            profile_slug: The partner's profile slug.

        Returns:
            JSON ``{keys: [{version, wrapped_key}], latest}`` (``latest`` is 0
            when no key exists yet).
        """
        profile = _get_profile(request)
        partner = get_object_or_404(Profile, slug=profile_slug)
        if partner.pk == profile.pk:
            return HttpResponseBadRequest("No self-conversations")
        low, high = ConversationKey.canonical_pair(profile, partner)
        rows = list(ConversationKey.objects.filter(profile_low=low, profile_high=high).order_by("version"))
        keys = [{"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)} for row in rows]
        return JsonResponse({"keys": keys, "latest": rows[-1].version if rows else 0})

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
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
            return HttpResponseBadRequest("No self-conversations")
        if not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            return JsonResponse({"error": "Not found."}, status=404)
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")

        wrapped_for_me = data.get("wrapped_for_me", "")
        wrapped_for_partner = data.get("wrapped_for_partner", "")
        if not valid_blob(wrapped_for_me, MAX_WRAPPED_CONVERSATION_KEY_LENGTH) or not valid_blob(wrapped_for_partner, MAX_WRAPPED_CONVERSATION_KEY_LENGTH):
            return HttpResponseBadRequest("Invalid wrapped key blobs")
        wrapped_for_low, wrapped_for_high = (wrapped_for_me, wrapped_for_partner) if profile.pk < partner.pk else (wrapped_for_partner, wrapped_for_me)

        if not MessagingKeyBundle.objects.filter(profile=profile).exists() or not MessagingKeyBundle.objects.filter(profile=partner).exists():
            return JsonResponse({"error": "Both participants must be enrolled."}, status=409)

        low, high = ConversationKey.canonical_pair(profile, partner)
        latest = ConversationKey.objects.filter(profile_low=low, profile_high=high).order_by("-version").first()
        expected_version = (latest.version if latest else 0) + 1
        try:
            requested_version = int(data.get("version", 0))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid version")
        if requested_version != expected_version:
            return JsonResponse({"error": f"Expected version {expected_version}.", "expected": expected_version}, status=409)

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
            return JsonResponse({"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)}, status=200)
        return JsonResponse({"version": row.version, "wrapped_key": row.wrapped_for(profile.pk)}, status=201)


class E2EERewrapView(LoginRequiredMixin, View):
    """POST: replace wrapped private-key copies (same key, new wrapping).

    Used after a password reset (re-wrap under the new password, clearing the
    stale flag) and when regenerating the recovery key. The private key itself
    never changes here - only which secrets can unwrap it.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Update wrapped copies on the caller's bundle.

        Args:
            request: JSON body with optional ``password_wrapped_secret`` +
                ``password_wrap_salt`` (together) and/or
                ``recovery_wrapped_secret``.

        Returns:
            JSON ``{ok: true}``; 400 on malformed blobs; 404 when not enrolled.
        """
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.filter(profile=profile).first()
        if bundle is None:
            return JsonResponse({"error": "Not enrolled."}, status=404)
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")

        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrapped_secret")
        if not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid password_wrap_salt")
        if not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid recovery_wrapped_secret")
        if bool(password_wrapped) != bool(password_wrap_salt):
            return HttpResponseBadRequest("password_wrapped_secret and password_wrap_salt must be provided together")
        if not password_wrapped and not recovery_wrapped:
            return HttpResponseBadRequest("Nothing to update")

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
        return JsonResponse({"ok": True})


class E2EEResetView(LoginRequiredMixin, View):
    """POST: replace the caller's keypair entirely (destructive, last resort).

    Old encrypted messages become permanently unreadable to the caller (the
    conversation partners keep their own copies - old ``ConversationKey``
    versions are retained for them). Requires a typed confirmation string.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Replace the caller's key bundle with brand-new key material.

        Args:
            request: JSON body with ``confirm`` (must equal ``"RESET"``),
                ``public_key``, ``recovery_wrapped_secret``, and optional
                ``password_wrapped_secret``/``password_wrap_salt``.

        Returns:
            JSON ``{version}``; 400 on malformed input or missing confirmation;
            404 when not enrolled.
        """
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.filter(profile=profile).first()
        if bundle is None:
            return JsonResponse({"error": "Not enrolled."}, status=404)
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")
        if data.get("confirm") != RESET_CONFIRMATION:
            return HttpResponseBadRequest("Missing confirmation")

        public_key = data.get("public_key", "")
        recovery_wrapped = data.get("recovery_wrapped_secret", "")
        password_wrapped = data.get("password_wrapped_secret", "")
        password_wrap_salt = data.get("password_wrap_salt", "")
        if not valid_blob(public_key, MAX_PUBLIC_KEY_LENGTH) or not valid_blob(recovery_wrapped, MAX_WRAPPED_SECRET_LENGTH):
            return HttpResponseBadRequest("Invalid key material")
        if not valid_blob(password_wrapped, MAX_WRAPPED_SECRET_LENGTH, required=False) or not valid_blob(password_wrap_salt, MAX_SALT_LENGTH, required=False):
            return HttpResponseBadRequest("Invalid key material")
        if bool(password_wrapped) != bool(password_wrap_salt):
            return HttpResponseBadRequest("password_wrapped_secret and password_wrap_salt must be provided together")

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
        logger.info("E2EE key reset for profile %s (now v%s)", profile.pk, bundle.version)
        return JsonResponse({"version": bundle.version})
