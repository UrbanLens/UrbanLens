"""Tests for direct-message end-to-end encryption.

Covers:
- The E2EE storage endpoints (login-params enumeration behavior, enroll
  idempotency + auth rotation, partner-key gating, conversation-key create +
  race, rewrap, reset).
- DirectMessage body/ciphertext mutual exclusivity in create_direct_message.
- Generic previews in the notification/email/serializer paths for encrypted
  messages (the server never sees plaintext).
- The export path emitting ciphertext + a note instead of a readable body.
- PyNaCl interop round-trips proving the documented blob formats match what
  the browser's libsodium produces (Argon2id derive, secretbox wrap, sealed
  box, message encrypt/decrypt).
"""

from __future__ import annotations

import base64
import json
import os
import tempfile

from django.test import Client
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account import AccountKdf
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.e2ee import ConversationKey, MessagingKeyBundle
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.direct_messages import create_direct_message, serialize_direct_message
from urbanlens.dashboard.services.e2ee import fake_auth_salt, is_base64, login_params_for_identifier, valid_blob

_db_settings = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _profile(*, username: str | None = None, password: str | None = None) -> Profile:
    user = baker.make("auth.User", username=username) if username else baker.make("auth.User")
    if password:
        user.set_password(password)
        user.save(update_fields=["password"])
    return user.profile


def _open_dms(a: Profile, b: Profile) -> None:
    """Let each of two profiles message the other (ANYONE visibility)."""
    Profile.objects.filter(pk__in=[a.pk, b.pk]).update(direct_message_visibility=VisibilityChoice.ANYONE)
    a.refresh_from_db()
    b.refresh_from_db()


def _enroll(profile: Profile, *, public_key: bytes | None = None) -> MessagingKeyBundle:
    return MessagingKeyBundle.objects.create(
        profile=profile,
        public_key=_b64(public_key or os.urandom(32)),
        recovery_wrapped_secret=_b64(os.urandom(72)),
    )


def _client_for(profile: Profile) -> Client:
    client = Client()
    client.force_login(profile.user)
    return client


# -- Service-layer helpers -------------------------------------------------------


class BlobValidationTests(SimpleTestCase):
    """is_base64 / valid_blob accept only well-formed, bounded base64."""

    def test_is_base64_rejects_empty_and_garbage(self) -> None:
        self.assertFalse(is_base64(""))
        self.assertFalse(is_base64("not base64!!!"))
        self.assertTrue(is_base64(_b64(b"hello")))

    def test_valid_blob_enforces_length(self) -> None:
        self.assertTrue(valid_blob(_b64(b"x" * 10), 64))
        self.assertFalse(valid_blob(_b64(b"x" * 100), 8))

    def test_valid_blob_optional(self) -> None:
        self.assertTrue(valid_blob("", 64, required=False))
        self.assertFalse(valid_blob("", 64))

    @_db_settings
    @given(raw=st.binary(min_size=1, max_size=48))
    def test_is_base64_roundtrip(self, raw: bytes) -> None:
        self.assertTrue(is_base64(_b64(raw)))


class LoginParamsTests(TestCase):
    """login-params never reveals whether an account exists."""

    def test_unknown_identifier_looks_derived(self) -> None:
        params = login_params_for_identifier("nobody-here@example.com")
        self.assertEqual(params["mode"], "derived")
        self.assertTrue(params["auth_salt"])

    def test_fake_salt_is_stable(self) -> None:
        self.assertEqual(fake_auth_salt("someone"), fake_auth_salt("someone"))
        self.assertNotEqual(fake_auth_salt("someone"), fake_auth_salt("else"))

    def test_legacy_account_reports_legacy(self) -> None:
        profile = _profile(username="legacy_user", password="pw")
        params = login_params_for_identifier("legacy_user")
        self.assertEqual(params["mode"], "legacy")
        self.assertEqual(params["auth_salt"], "")
        self.assertTrue(profile)  # keep the account alive for the query

    def test_enrolled_account_reports_real_salt(self) -> None:
        profile = _profile(username="derived_user", password="pw")
        AccountKdf.objects.create(user=profile.user, auth_salt=_b64(os.urandom(16)))
        params = login_params_for_identifier("derived_user")
        self.assertEqual(params["mode"], "derived")
        self.assertEqual(params["auth_salt"], AccountKdf.objects.get(user=profile.user).auth_salt)


# -- Endpoints -------------------------------------------------------------------


class EnrollEndpointTests(TestCase):
    """enroll stores the bundle, rotates auth with password proof, is idempotent."""

    def _payload(self, **overrides) -> dict:
        payload = {
            "public_key": _b64(os.urandom(32)),
            "recovery_wrapped_secret": _b64(os.urandom(72)),
            "kdf_opslimit": 2,
            "kdf_memlimit": 67108864,
        }
        payload.update(overrides)
        return payload

    def test_oauth_enroll_without_password(self) -> None:
        profile = _profile()
        response = _client_for(profile).post(reverse("e2ee.enroll"), data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(MessagingKeyBundle.objects.filter(profile=profile).exists())
        self.assertFalse(AccountKdf.objects.filter(user=profile.user).exists())

    def test_password_account_enroll_requires_current_password_proof(self) -> None:
        profile = _profile(password="correct-horse")
        response = _client_for(profile).post(reverse("e2ee.enroll"), data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MessagingKeyBundle.objects.filter(profile=profile).exists())

    def test_password_account_enroll_accepts_current_password_proof_without_rotation(self) -> None:
        profile = _profile(password="correct-horse")
        response = _client_for(profile).post(
            reverse("e2ee.enroll"),
            data=json.dumps(self._payload(current_password="correct-horse")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(MessagingKeyBundle.objects.filter(profile=profile).exists())

    def test_derived_account_enroll_accepts_current_auth_credential(self) -> None:
        profile = _profile(password="initial")
        auth_key = _b64(os.urandom(32))
        profile.user.set_password(auth_key)
        profile.user.save(update_fields=["password"])
        AccountKdf.objects.create(user=profile.user, auth_salt=_b64(os.urandom(16)))

        response = _client_for(profile).post(
            reverse("e2ee.enroll"),
            data=json.dumps(self._payload(current_password=auth_key)),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(MessagingKeyBundle.objects.filter(profile=profile).exists())

    def test_enroll_is_idempotent(self) -> None:
        profile = _profile()
        client = _client_for(profile)
        first = client.post(reverse("e2ee.enroll"), data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(first.status_code, 201)
        second = client.post(reverse("e2ee.enroll"), data=json.dumps(self._payload()), content_type="application/json")
        self.assertEqual(second.status_code, 409)

    def test_password_rotation_requires_correct_current_password(self) -> None:
        profile = _profile(password="correct-horse")
        payload = self._payload(
            password_wrapped_secret=_b64(os.urandom(72)),
            password_wrap_salt=_b64(os.urandom(16)),
            auth_key=_b64(os.urandom(32)),
            auth_salt=_b64(os.urandom(16)),
            current_password="wrong",
        )
        response = _client_for(profile).post(reverse("e2ee.enroll"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MessagingKeyBundle.objects.filter(profile=profile).exists())

    def test_password_rotation_replaces_credential(self) -> None:
        profile = _profile(password="correct-horse")
        auth_key = _b64(os.urandom(32))
        payload = self._payload(
            password_wrapped_secret=_b64(os.urandom(72)),
            password_wrap_salt=_b64(os.urandom(16)),
            auth_key=auth_key,
            auth_salt=_b64(os.urandom(16)),
            current_password="correct-horse",
        )
        response = _client_for(profile).post(reverse("e2ee.enroll"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.check_password(auth_key))
        self.assertTrue(AccountKdf.objects.filter(user=profile.user).exists())

    def test_rejects_malformed_public_key(self) -> None:
        profile = _profile()
        response = _client_for(profile).post(
            reverse("e2ee.enroll"),
            data=json.dumps(self._payload(public_key="not base64!")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class OwnKeysEndpointTests(TestCase):
    """The caller's own "keys" endpoint always returns 200, not 404.

    Not being enrolled is a common, expected state (checked unconditionally
    on every page load to render the encryption status indicator) - it must
    not surface as an HTTP error status, which would show up as a spurious
    error in the browser console for most accounts on every page view.
    """

    def test_not_enrolled_reports_200_with_enrolled_false(self) -> None:
        profile = _profile()
        response = _client_for(profile).get(reverse("e2ee.keys"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enrolled": False})

    def test_enrolled_reports_the_bundle(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        response = _client_for(profile).get(reverse("e2ee.keys"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["enrolled"])
        self.assertEqual(payload["public_key"], bundle.public_key)


class PartnerKeyEndpointTests(TestCase):
    """Partner public keys are exposed only to profiles that may message them."""

    def test_returns_public_key_when_messaging_allowed(self) -> None:
        me, partner = _profile(), _profile()
        _open_dms(me, partner)
        bundle = _enroll(partner)
        response = _client_for(me).get(reverse("e2ee.partner_key", kwargs={"profile_slug": partner.ensure_slug()}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["public_key"], bundle.public_key)

    def test_404_when_messaging_forbidden(self) -> None:
        me, partner = _profile(), _profile()
        Profile.objects.filter(pk__in=[me.pk, partner.pk]).update(direct_message_visibility=VisibilityChoice.NO_ONE)
        _enroll(partner)
        response = _client_for(me).get(reverse("e2ee.partner_key", kwargs={"profile_slug": partner.ensure_slug()}))
        self.assertEqual(response.status_code, 404)


class ConversationKeyEndpointTests(TestCase):
    """Conversation keys are created once per pair with race handling."""

    def _create(self, client: Client, partner: Profile, version: int = 1) -> object:
        return client.post(
            reverse("e2ee.conversation_key", kwargs={"profile_slug": partner.ensure_slug()}),
            data=json.dumps({"version": version, "wrapped_for_me": _b64(os.urandom(48)), "wrapped_for_partner": _b64(os.urandom(48))}),
            content_type="application/json",
        )

    def test_create_and_fetch_roundtrip(self) -> None:
        me, partner = _profile(), _profile()
        _open_dms(me, partner)
        _enroll(me)
        _enroll(partner)
        created = self._create(_client_for(me), partner)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(ConversationKey.objects.count(), 1)

        fetched = _client_for(me).get(reverse("e2ee.conversation_key", kwargs={"profile_slug": partner.ensure_slug()}))
        self.assertEqual(fetched.status_code, 200)
        body = fetched.json()
        self.assertEqual(body["latest"], 1)
        self.assertEqual(body["keys"][0]["wrapped_key"], created.json()["wrapped_key"])

    def test_partner_fetches_their_own_wrap(self) -> None:
        me, partner = _profile(), _profile()
        _open_dms(me, partner)
        _enroll(me)
        _enroll(partner)
        self._create(_client_for(me), partner)
        partner_view = _client_for(partner).get(reverse("e2ee.conversation_key", kwargs={"profile_slug": me.ensure_slug()}))
        self.assertEqual(partner_view.status_code, 200)
        # Partner gets the "other side" of the same row - stored in canonical order.
        self.assertEqual(partner_view.json()["latest"], 1)

    def test_duplicate_version_conflicts(self) -> None:
        me, partner = _profile(), _profile()
        _open_dms(me, partner)
        _enroll(me)
        _enroll(partner)
        self._create(_client_for(me), partner)
        again = self._create(_client_for(me), partner, version=1)
        self.assertEqual(again.status_code, 409)

    def test_requires_both_enrolled(self) -> None:
        me, partner = _profile(), _profile()
        _open_dms(me, partner)
        _enroll(me)  # partner not enrolled
        response = self._create(_client_for(me), partner)
        self.assertEqual(response.status_code, 409)


class RewrapAndResetTests(TestCase):
    """rewrap clears the stale flag; reset bumps the bundle version."""

    def test_rewrap_clears_stale(self) -> None:
        profile = _profile(password="pw")
        bundle = _enroll(profile)
        MessagingKeyBundle.objects.filter(pk=bundle.pk).update(password_wrap_stale=True)
        response = _client_for(profile).post(
            reverse("e2ee.rewrap"),
            data=json.dumps({"password_wrapped_secret": _b64(os.urandom(72)), "password_wrap_salt": _b64(os.urandom(16)), "current_password": "pw"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        bundle.refresh_from_db()
        self.assertFalse(bundle.password_wrap_stale)

    def test_rewrap_password_copy_requires_current_password_proof(self) -> None:
        profile = _profile(password="pw")
        bundle = _enroll(profile)
        response = _client_for(profile).post(
            reverse("e2ee.rewrap"),
            data=json.dumps({"password_wrapped_secret": _b64(os.urandom(72)), "password_wrap_salt": _b64(os.urandom(16))}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        bundle.refresh_from_db()
        self.assertEqual(bundle.password_wrapped_secret, "")

    def test_reset_requires_confirmation(self) -> None:
        profile = _profile()
        _enroll(profile)
        response = _client_for(profile).post(
            reverse("e2ee.reset"),
            data=json.dumps({"public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72))}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reset_bumps_version(self) -> None:
        profile = _profile()
        _enroll(profile)
        response = _client_for(profile).post(
            reverse("e2ee.reset"),
            data=json.dumps({"confirm": "RESET", "public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72))}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(response.json()["rewrapped"], 0)

    def test_password_account_reset_requires_current_password_proof(self) -> None:
        profile = _profile(password="pw")
        bundle = _enroll(profile)
        response = _client_for(profile).post(
            reverse("e2ee.reset"),
            data=json.dumps({"confirm": "RESET", "public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72))}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        bundle.refresh_from_db()
        self.assertEqual(bundle.version, 1)

    def test_password_account_reset_accepts_current_password_proof(self) -> None:
        profile = _profile(password="pw")
        _enroll(profile)
        response = _client_for(profile).post(
            reverse("e2ee.reset"),
            data=json.dumps({"confirm": "RESET", "public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72)), "current_password": "pw"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 2)


class RewrapAllAndResetPreservationTests(TestCase):
    """rewrap-all lists only the caller's copies; reset applies re-sealed copies atomically."""

    def _pair_key(self, a: Profile, b: Profile) -> ConversationKey:
        low, high = (a, b) if a.pk < b.pk else (b, a)
        return ConversationKey.objects.create(
            profile_low=low,
            profile_high=high,
            wrapped_for_low=_b64(os.urandom(48)),
            wrapped_for_high=_b64(os.urandom(48)),
            version=1,
        )

    def _group_envelope(self, profile: Profile):
        from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope
        from urbanlens.dashboard.models.group_chats.model import GroupChat

        group: GroupChat = baker.make("dashboard.GroupChat")
        key = GroupKey.objects.create(group=group, version=1)
        return GroupKeyEnvelope.objects.create(key=key, profile=profile, wrapped_key=_b64(os.urandom(48)))

    def test_rewrap_all_requires_enrollment(self) -> None:
        profile = _profile()
        response = _client_for(profile).get(reverse("e2ee.rewrap_all"))
        self.assertEqual(response.status_code, 404)

    def test_rewrap_all_lists_only_the_callers_copies(self) -> None:
        me, partner, other = _profile(), _profile(), _profile()
        _enroll(me)
        mine = self._pair_key(me, partner)
        self._pair_key(partner, other)  # not mine - must not appear
        my_envelope = self._group_envelope(me)
        self._group_envelope(partner)  # not mine either

        payload = _client_for(me).get(reverse("e2ee.rewrap_all")).json()
        self.assertEqual([entry["id"] for entry in payload["conversation_keys"]], [mine.pk])
        self.assertEqual(payload["conversation_keys"][0]["wrapped_key"], mine.wrapped_for(me.pk))
        self.assertEqual([entry["id"] for entry in payload["group_envelopes"]], [my_envelope.pk])
        self.assertEqual(payload["group_envelopes"][0]["wrapped_key"], my_envelope.wrapped_key)

    def test_rewrap_all_returns_the_high_side_for_the_high_participant(self) -> None:
        a, b = _profile(), _profile()
        high = a if a.pk > b.pk else b
        _enroll(high)
        row = self._pair_key(a, b)

        payload = _client_for(high).get(reverse("e2ee.rewrap_all")).json()
        self.assertEqual(payload["conversation_keys"][0]["wrapped_key"], row.wrapped_for_high)

    def _reset_body(self, **extra) -> str:
        return json.dumps({"confirm": "RESET", "public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72)), **extra})

    def test_reset_applies_rewrapped_copies_and_reports_the_count(self) -> None:
        me, partner = _profile(), _profile()
        _enroll(me)
        row = self._pair_key(me, partner)
        envelope = self._group_envelope(me)
        partner_side_before = row.wrapped_for(partner.pk)
        new_conv_blob = _b64(os.urandom(48))
        new_env_blob = _b64(os.urandom(48))

        response = _client_for(me).post(
            reverse("e2ee.reset"),
            data=self._reset_body(
                rewrapped_conversation_keys=[{"id": row.pk, "wrapped_key": new_conv_blob}],
                rewrapped_group_envelopes=[{"id": envelope.pk, "wrapped_key": new_env_blob}],
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rewrapped"], 2)
        row.refresh_from_db()
        envelope.refresh_from_db()
        self.assertEqual(row.wrapped_for(me.pk), new_conv_blob)
        # The partner's sealed copy must be untouchable from this endpoint.
        self.assertEqual(row.wrapped_for(partner.pk), partner_side_before)
        self.assertEqual(envelope.wrapped_key, new_env_blob)

    def test_reset_rejects_a_foreign_conversation_key_id_without_applying_anything(self) -> None:
        me, partner, other = _profile(), _profile(), _profile()
        bundle = _enroll(me)
        foreign = self._pair_key(partner, other)
        foreign_blob_before = foreign.wrapped_for_low

        response = _client_for(me).post(
            reverse("e2ee.reset"),
            data=self._reset_body(rewrapped_conversation_keys=[{"id": foreign.pk, "wrapped_key": _b64(os.urandom(48))}]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        bundle.refresh_from_db()
        foreign.refresh_from_db()
        # Atomicity: the rejected request must not have swapped the bundle
        # or written the foreign row.
        self.assertEqual(bundle.version, 1)
        self.assertEqual(foreign.wrapped_for_low, foreign_blob_before)

    def test_reset_rejects_a_foreign_group_envelope_id(self) -> None:
        me, other = _profile(), _profile()
        bundle = _enroll(me)
        foreign = self._group_envelope(other)

        response = _client_for(me).post(
            reverse("e2ee.reset"),
            data=self._reset_body(rewrapped_group_envelopes=[{"id": foreign.pk, "wrapped_key": _b64(os.urandom(48))}]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        bundle.refresh_from_db()
        self.assertEqual(bundle.version, 1)

    def test_reset_rejects_a_malformed_rewrap_entry(self) -> None:
        me = _profile()
        _enroll(me)
        response = _client_for(me).post(
            reverse("e2ee.reset"),
            data=self._reset_body(rewrapped_conversation_keys=[{"id": 1, "wrapped_key": "not base64!!!"}]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


# -- create_direct_message with ciphertext ---------------------------------------


class EncryptedCreateTests(TestCase):
    """Encrypted messages store ciphertext, never a readable body."""

    def _pair(self) -> tuple[Profile, Profile]:
        a, b = _profile(), _profile()
        _open_dms(a, b)
        return a, b

    def test_encrypted_message_persists_ciphertext(self) -> None:
        a, b = self._pair()
        message = create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1)
        self.assertTrue(message.is_encrypted)
        self.assertEqual(message.body, "")

    def test_body_and_ciphertext_are_mutually_exclusive(self) -> None:
        a, b = self._pair()
        with self.assertRaises(ValueError):
            create_direct_message(a, b, "hi", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1)

    def test_ciphertext_requires_nonce_and_version(self) -> None:
        a, b = self._pair()
        with self.assertRaises(ValueError):
            create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce="", key_version=1)
        with self.assertRaises(ValueError):
            create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=0)

    def test_serializer_passes_ciphertext_and_generic_reply_preview(self) -> None:
        a, b = self._pair()
        first = create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1)
        reply = create_direct_message(b, a, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1, reply_to_id=first.pk)
        payload = serialize_direct_message(reply)
        self.assertEqual(payload["body"], "")
        self.assertTrue(payload["ciphertext"])
        self.assertEqual(payload["reply_to"]["preview"], "🔒 Message")
        self.assertTrue(payload["reply_to"]["ciphertext"])

    def test_notification_preview_is_generic_for_encrypted(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import NotificationType
        from urbanlens.dashboard.models.notifications.model import NotificationLog

        a, b = self._pair()
        create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1)
        note = NotificationLog.objects.filter(profile=b, notification_type=NotificationType.MESSAGE).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.message, "🔒 Encrypted message")


# -- Export ----------------------------------------------------------------------


class ExportTests(TestCase):
    """Encrypted messages export ciphertext + a note, not a readable body."""

    def test_encrypted_message_exports_ciphertext(self) -> None:
        from urbanlens.dashboard.services.export import _export_direct_messages

        a, b = _profile(), _profile()
        _open_dms(a, b)
        create_direct_message(a, b, "", ciphertext=_b64(os.urandom(64)), nonce=_b64(os.urandom(24)), key_version=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_direct_messages(a, temp_dir)
            with open(os.path.join(temp_dir, "direct_messages.json"), encoding="utf-8") as fh:
                rows = json.load(fh)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["body"])
        self.assertTrue(rows[0]["encrypted"])
        self.assertTrue(rows[0]["ciphertext"])
        self.assertIn("note", rows[0])


# -- Conversation-key GET gating --------------------------------------------------


class ConversationKeyGetOracleTests(TestCase):
    """The conversation-key GET must not be a profile-slug existence oracle.

    Regression: the GET returned 200-with-empty-keys for any existing profile
    slug (no relationship required) and a 404 only for unknown slugs, letting
    a logged-in user enumerate which slugs exist - which ConversationView
    deliberately prevents. With no keys and no permitted DM relationship in
    either direction, the response must now be the same 404 an unknown slug
    produces. Existing keys stay fetchable regardless of the current
    relationship, because a participant must still decrypt their history
    after a block or privacy change.
    """

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.stranger = _profile()  # default visibility, nothing in common: no DM permitted either way
        self.stranger.ensure_slug()
        self.client.force_login(self.me.user)

    def _get(self, slug: str):
        return self.client.get(reverse("e2ee.conversation_key", kwargs={"profile_slug": slug}))

    def test_unrelated_existing_slug_matches_unknown_slug(self) -> None:
        existing = self._get(self.stranger.slug)
        unknown = self._get("no-such-profile-slug")
        self.assertEqual(existing.status_code, 404)
        self.assertEqual(unknown.status_code, 404)

    def test_messageable_partner_without_keys_still_gets_empty_payload(self) -> None:
        _open_dms(self.me, self.stranger)
        response = self._get(self.stranger.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"keys": [], "latest": 0})

    def test_existing_keys_stay_fetchable_without_a_current_relationship(self) -> None:
        low, high = (self.me, self.stranger) if self.me.pk < self.stranger.pk else (self.stranger, self.me)
        ConversationKey.objects.create(
            profile_low=low,
            profile_high=high,
            wrapped_for_low=_b64(b"low-copy"),
            wrapped_for_high=_b64(b"high-copy"),
            version=1,
        )
        response = self._get(self.stranger.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["latest"], 1)
