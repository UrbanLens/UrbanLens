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
from django.http import Http404, HttpResponseBadRequest, JsonResponse
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

        if MessagingKeyBundle.objects.for_profile(profile).exists():
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
        profile = _get_profile(request)
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
        if bundle is None:
            return JsonResponse({"enrolled": False})
        return JsonResponse(
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
        bundle = MessagingKeyBundle.objects.for_profile(partner).first()
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
            return HttpResponseBadRequest("No self-conversations")
        rows = list(ConversationKey.objects.between(profile, partner))
        if not rows and not can_direct_message(profile, partner) and not can_direct_message(partner, profile):
            raise Http404
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
        low, high = ConversationKey.canonical_pair(profile, partner)

        if not MessagingKeyBundle.objects.for_profile(profile).exists() or not MessagingKeyBundle.objects.for_profile(partner).exists():
            return JsonResponse({"error": "Both participants must be enrolled."}, status=409)

        latest = ConversationKey.objects.between(profile, partner).order_by("-version").first()
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
        bundle = MessagingKeyBundle.objects.for_profile(profile).first()
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


class E2EEGroupKeyView(LoginRequiredMixin, View):
    """GET/POST the wrapped group-key versions for one group chat.

    GET returns only the caller's own envelopes (one per version they were a
    member for), plus what a client needs to rotate: the latest version
    number, whether that version still covers the group's current membership,
    and - when every member is enrolled - each member's public key.

    POST stores the next version: the creating client generates the random
    key and seals it once per active member; the server verifies the envelope
    set covers the active membership exactly and stores blobs it cannot open.
    """

    def _resolve(self, request: HttpRequest, group_uuid) -> tuple[Profile, Any, Any] | None:
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

    def get(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Return the caller's envelopes and the group's rotation state.

        Args:
            request: The authenticated request.
            group_uuid: UUID of the group chat.

        Returns:
            JSON ``{keys, latest, needs_rotation, members}`` - ``members`` is
            a ``[{id, public_key}]`` list when every active member is enrolled
            (so the caller can rotate), else null. ``id`` is an opaque
            per-(group, member) token (see ``services.e2ee.group_member_token``)
            - never a slug, which would hand every member the real identity of
            members whose ``profile_visibility`` masks them elsewhere. 404 for
            non-members and unknown groups.
        """
        from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope, MessagingKeyBundle
        from urbanlens.dashboard.services.e2ee import group_member_token

        resolved = self._resolve(request, group_uuid)
        if resolved is None:
            return JsonResponse({"error": "Not found."}, status=404)
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
        return JsonResponse({"keys": keys, "latest": latest, "needs_rotation": needs_rotation, "members": members})

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
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
        from urbanlens.dashboard.services.e2ee import group_member_token

        resolved = self._resolve(request, group_uuid)
        if resolved is None:
            return JsonResponse({"error": "Not found."}, status=404)
        profile, group, _membership = resolved
        data = _json_body(request)
        if data is None:
            return HttpResponseBadRequest("Malformed JSON body")

        wrapped = data.get("wrapped")
        if not isinstance(wrapped, dict) or not wrapped:
            return HttpResponseBadRequest("Invalid wrapped envelopes")

        # Keyed by opaque per-(group, member) tokens, recomputed here rather
        # than decoded - the client just round-trips the ids the GET response
        # issued. A stale client still keying by slug gets the same 409 as any
        # other membership mismatch and retries after refetching.
        members_by_token = {group_member_token(group.uuid, membership.profile_id): membership.profile for membership in group.active_memberships().select_related("profile", "profile__user")}
        if set(wrapped) != set(members_by_token):
            return JsonResponse({"error": "Envelopes must cover the group's current members exactly."}, status=409)
        for blob in wrapped.values():
            if not valid_blob(blob, MAX_WRAPPED_CONVERSATION_KEY_LENGTH):
                return HttpResponseBadRequest("Invalid wrapped envelopes")
        enrolled_count = MessagingKeyBundle.objects.for_profiles(members_by_token.values()).count()
        if enrolled_count != len(members_by_token):
            return JsonResponse({"error": "Every member must be enrolled before the group can encrypt."}, status=409)

        latest = GroupKey.objects.for_group(group).order_by("-version").first()
        expected_version = (latest.version if latest else 0) + 1
        try:
            requested_version = int(data.get("version", 0))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid version")
        if requested_version != expected_version:
            return JsonResponse({"error": f"Expected version {expected_version}.", "expected": expected_version}, status=409)

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
                return JsonResponse({"error": "No envelope for this member."}, status=409)
            return JsonResponse({"version": winner.version, "wrapped_key": envelope.wrapped_key}, status=200)
        return JsonResponse({"version": key_row.version, "wrapped_key": wrapped[group_member_token(group.uuid, profile.pk)]}, status=201)


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


class E2EERewrapAllView(LoginRequiredMixin, View):
    """GET: every wrapped key copy addressed to the caller, for bulk re-wrap.

    Used by the reset flow when the client still holds (or can unlock) the
    OLD private key: it unseals each copy locally, re-seals it to the new
    public key, and submits the results alongside the reset so the caller's
    message history stays readable. Returns only blobs the caller could
    already fetch one conversation/group at a time - this just avoids N
    round trips.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
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
            return JsonResponse({"error": "Not enrolled."}, status=404)

        conversation_keys = [{"id": row.pk, "wrapped_key": row.wrapped_for(profile.pk)} for row in ConversationKey.objects.filter(Q(profile_low=profile) | Q(profile_high=profile))]
        group_envelopes = [{"id": envelope.pk, "wrapped_key": envelope.wrapped_key} for envelope in GroupKeyEnvelope.objects.filter(profile=profile)]
        return JsonResponse({"conversation_keys": conversation_keys, "group_envelopes": group_envelopes})


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


class E2EEResetView(LoginRequiredMixin, View):
    """POST: replace the caller's keypair entirely (last resort).

    When the client still holds the old private key it submits re-sealed
    copies of every conversation/group key alongside the reset, and the
    caller's message history stays readable under the new keypair. Without
    them, old encrypted messages become permanently unreadable to the caller
    (conversation partners keep their own copies - old ``ConversationKey``
    versions are retained for them). Requires a typed confirmation string.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
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

        rewrapped_conversations = _parse_rewrap_entries(data.get("rewrapped_conversation_keys"))
        rewrapped_envelopes = _parse_rewrap_entries(data.get("rewrapped_group_envelopes"))
        if rewrapped_conversations is None or rewrapped_envelopes is None:
            return HttpResponseBadRequest("Invalid rewrapped key entries")

        # Resolve every submitted id to a row the caller actually owns BEFORE
        # writing anything - a single foreign/unknown id rejects the whole
        # request rather than partially applying it.
        conversation_rows = []
        if rewrapped_conversations:
            conversation_rows = list(
                ConversationKey.objects.filter(Q(profile_low=profile) | Q(profile_high=profile), pk__in=rewrapped_conversations),
            )
            if len(conversation_rows) != len(rewrapped_conversations):
                return HttpResponseBadRequest("Unknown conversation key id")
        envelope_rows = []
        if rewrapped_envelopes:
            envelope_rows = list(GroupKeyEnvelope.objects.filter(profile=profile, pk__in=rewrapped_envelopes))
            if len(envelope_rows) != len(rewrapped_envelopes):
                return HttpResponseBadRequest("Unknown group envelope id")

        with transaction.atomic():
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
        logger.info("E2EE key reset for profile %s (now v%s, %s key copies re-wrapped)", profile.pk, bundle.version, rewrapped_count)
        return JsonResponse({"version": bundle.version, "rewrapped": rewrapped_count})
