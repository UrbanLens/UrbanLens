"""Tests for the passkey-PRF unlock layer (docs/designs/e2ee-passkey-unlock.md).

Three properties carry the design and each gets direct cover here:

1. **Unlock-only passkeys never change how the account signs in** - every 2FA
   gate filters on ``is_login_factor``, so enrolling a key to decrypt messages
   must not conscript the user into a login challenge (and an assertion from
   such a key must not *complete* a login either).
2. **The wrap endpoints demand the password proof** on password-backed
   accounts - adding or destroying an unlock path must cost more than a bearer
   token or a bare session.
3. **Wraps die with the keypair** - a reset purges them atomically, and the
   keys endpoint never serves a wrap whose ``bundle_version`` lags the bundle.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import os

from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import WebAuthnCredential
from urbanlens.dashboard.models.e2ee import E2EEPasskeyWrap, MessagingKeyBundle
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.two_factor import has_second_factor
from urbanlens.dashboard.services.auth.webauthn import (
    SESSION_AUTHENTICATION_CHALLENGE,
    WebAuthnError,
    build_authentication_options,
    build_registration_options,
    has_passkeys,
    verify_authentication,
)

PASSWORD = "a-long-test-password-123"  # noqa: S105 - test fixture, not a real credential


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _profile(*, password: str | None = None) -> Profile:
    user = baker.make("auth.User")
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.save(update_fields=["password"])
    return user.profile


def _enroll(profile: Profile) -> MessagingKeyBundle:
    return MessagingKeyBundle.objects.create(
        profile=profile,
        public_key=_b64(os.urandom(32)),
        recovery_wrapped_secret=_b64(os.urandom(72)),
    )


def _credential(profile: Profile, *, login_factor: bool = True) -> WebAuthnCredential:
    return baker.make(
        WebAuthnCredential,
        user=profile.user,
        credential_id=os.urandom(32),
        public_key=os.urandom(64),
        is_login_factor=login_factor,
    )


def _wrap(bundle: MessagingKeyBundle, credential: WebAuthnCredential, *, version: int | None = None) -> E2EEPasskeyWrap:
    return E2EEPasskeyWrap.objects.create(
        credential=credential,
        bundle=bundle,
        prf_input=_b64(os.urandom(32)),
        wrapped_secret=_b64(os.urandom(72)),
        bundle_version=bundle.version if version is None else version,
    )


def _client_for(profile: Profile) -> Client:
    client = Client()
    client.force_login(profile.user)
    return client


def _wrap_payload(credential: WebAuthnCredential, **extra: object) -> str:
    return json.dumps(
        {
            "credential_id": _b64url(bytes(credential.credential_id)),
            "prf_input": _b64(os.urandom(32)),
            "wrapped_secret": _b64(os.urandom(72)),
            **extra,
        },
    )


class UnlockOnlyPasskeysAreNotLoginFactorsTests(TestCase):
    """Property 1: enrolling an unlock key never flips the account into 2FA."""

    def test_unlock_only_credential_is_not_a_second_factor(self) -> None:
        profile = _profile()
        _credential(profile, login_factor=False)

        self.assertFalse(has_passkeys(profile.user))
        self.assertFalse(has_second_factor(profile.user))

    def test_login_factor_credential_still_is(self) -> None:
        profile = _profile()
        _credential(profile, login_factor=True)

        self.assertTrue(has_passkeys(profile.user))
        self.assertTrue(has_second_factor(profile.user))

    def test_authentication_options_exclude_unlock_only_credentials(self) -> None:
        profile = _profile()
        login_cred = _credential(profile, login_factor=True)
        unlock_cred = _credential(profile, login_factor=False)
        request = RequestFactory().get("/")
        request.session = {}

        options = json.loads(build_authentication_options(request, profile.user))

        offered = {entry["id"] for entry in options["allowCredentials"]}
        self.assertIn(_b64url(bytes(login_cred.credential_id)), offered)
        self.assertNotIn(_b64url(bytes(unlock_cred.credential_id)), offered)

    def test_authentication_options_refuse_an_unlock_only_account(self) -> None:
        profile = _profile()
        _credential(profile, login_factor=False)
        request = RequestFactory().get("/")
        request.session = {}

        with pytest.raises(WebAuthnError):
            build_authentication_options(request, profile.user)

    def test_unlock_only_assertion_cannot_complete_login(self) -> None:
        """The lookup filter is the backstop should a client post one anyway."""
        profile = _profile()
        unlock_cred = _credential(profile, login_factor=False)
        request = RequestFactory().post("/")
        request.session = {SESSION_AUTHENTICATION_CHALLENGE: _b64url(os.urandom(32))}
        assertion = json.dumps({"rawId": _b64url(bytes(unlock_cred.credential_id))})

        with pytest.raises(WebAuthnError, match="not registered"):
            verify_authentication(request, profile.user, assertion)


class PrfExtensionInCeremonyOptionsTests(TestCase):
    """The prf extension rides the ceremonies exactly where wraps exist."""

    def test_registration_options_always_request_prf(self) -> None:
        profile = _profile()
        request = RequestFactory().post("/")
        request.session = {}

        options = json.loads(build_registration_options(request, profile.user))

        # Enable-only ({}): any new passkey can later gain a wrap.
        self.assertEqual(options["extensions"]["prf"], {})

    def test_login_options_carry_prf_inputs_for_wrapped_credentials(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        wrapped_cred = _credential(profile, login_factor=True)
        bare_cred = _credential(profile, login_factor=True)
        wrap = _wrap(bundle, wrapped_cred)
        request = RequestFactory().get("/")
        request.session = {}

        options = json.loads(build_authentication_options(request, profile.user))

        eval_by_credential = options["extensions"]["prf"]["evalByCredential"]
        self.assertEqual(eval_by_credential[_b64url(bytes(wrapped_cred.credential_id))]["first"], wrap.prf_input)
        self.assertNotIn(_b64url(bytes(bare_cred.credential_id)), eval_by_credential)

    def test_login_options_omit_prf_without_wraps(self) -> None:
        profile = _profile()
        _credential(profile, login_factor=True)
        request = RequestFactory().get("/")
        request.session = {}

        options = json.loads(build_authentication_options(request, profile.user))

        self.assertNotIn("extensions", options)

    def test_stale_wraps_do_not_ride_the_login_assertion(self) -> None:
        """A wrap sealed to a superseded keypair would unlock the wrong identity."""
        profile = _profile()
        bundle = _enroll(profile)
        credential = _credential(profile, login_factor=True)
        _wrap(bundle, credential, version=bundle.version - 1 or 0)
        request = RequestFactory().get("/")
        request.session = {}

        options = json.loads(build_authentication_options(request, profile.user))

        self.assertNotIn("extensions", options)


class PasskeyWrapEndpointTests(TestCase):
    """Property 2: creating/destroying an unlock path costs the password proof."""

    def test_oauth_only_account_creates_a_wrap(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        credential = _credential(profile, login_factor=False)

        response = _client_for(profile).post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential), content_type="application/json")

        self.assertEqual(response.status_code, 201)
        wrap = E2EEPasskeyWrap.objects.get(credential=credential)
        self.assertEqual(wrap.bundle, bundle)
        self.assertEqual(wrap.bundle_version, bundle.version)

    def test_password_account_requires_the_proof(self) -> None:
        profile = _profile(password=PASSWORD)
        _enroll(profile)
        credential = _credential(profile)
        client = _client_for(profile)

        missing = client.post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential), content_type="application/json")
        wrong = client.post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential, current_password="nope"), content_type="application/json")  # noqa: S106 - deliberately wrong test credential
        right = client.post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential, current_password=PASSWORD), content_type="application/json")

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(right.status_code, 201)

    def test_someone_elses_credential_is_rejected(self) -> None:
        profile = _profile()
        _enroll(profile)
        other = _profile()
        foreign_credential = _credential(other)

        response = _client_for(profile).post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(foreign_credential), content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(E2EEPasskeyWrap.objects.exists())

    def test_reenrolling_replaces_the_wrap(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        credential = _credential(profile)
        old = _wrap(bundle, credential)
        client = _client_for(profile)

        response = client.post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        wrap = E2EEPasskeyWrap.objects.get(credential=credential)
        self.assertNotEqual(wrap.wrapped_secret, old.wrapped_secret)

    def test_requires_enrollment(self) -> None:
        profile = _profile()
        credential = _credential(profile)

        response = _client_for(profile).post(reverse("e2ee.passkey_wrap"), data=_wrap_payload(credential), content_type="application/json")

        self.assertEqual(response.status_code, 404)

    def test_malformed_blobs_are_rejected(self) -> None:
        profile = _profile()
        _enroll(profile)
        credential = _credential(profile)
        client = _client_for(profile)

        bad_input = json.loads(_wrap_payload(credential))
        bad_input["prf_input"] = "not base64!!!"
        bad_secret = json.loads(_wrap_payload(credential))
        bad_secret["wrapped_secret"] = ""

        for payload in (bad_input, bad_secret):
            with self.subTest(payload=payload):
                response = client.post(reverse("e2ee.passkey_wrap"), data=json.dumps(payload), content_type="application/json")
                self.assertEqual(response.status_code, 400)

    def test_delete_removes_the_wrap_with_proof(self) -> None:
        profile = _profile(password=PASSWORD)
        bundle = _enroll(profile)
        credential = _credential(profile)
        _wrap(bundle, credential)
        client = _client_for(profile)
        url = reverse("e2ee.passkey_wrap_delete", kwargs={"credential_id": _b64url(bytes(credential.credential_id))})

        unproven = client.delete(url, data=json.dumps({}), content_type="application/json")
        proven = client.delete(url, data=json.dumps({"current_password": PASSWORD}), content_type="application/json")

        self.assertEqual(unproven.status_code, 403)
        self.assertEqual(proven.status_code, 200)
        self.assertFalse(E2EEPasskeyWrap.objects.filter(credential=credential).exists())

    def test_delete_of_a_wrapless_credential_is_404(self) -> None:
        profile = _profile()
        _enroll(profile)
        credential = _credential(profile)

        response = _client_for(profile).delete(
            reverse("e2ee.passkey_wrap_delete", kwargs={"credential_id": _b64url(bytes(credential.credential_id))}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)


class OwnKeysPasskeyPayloadTests(TestCase):
    """The keys endpoint serves wraps and the credential roster for enrollment UI."""

    def test_usable_wraps_and_credentials_are_listed(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        wrapped_cred = _credential(profile, login_factor=True)
        bare_cred = _credential(profile, login_factor=False)
        wrap = _wrap(bundle, wrapped_cred)

        payload = _client_for(profile).get(reverse("e2ee.keys")).json()

        self.assertEqual(len(payload["passkey_wraps"]), 1)
        self.assertEqual(payload["passkey_wraps"][0]["credential_id"], _b64url(bytes(wrapped_cred.credential_id)))
        self.assertEqual(payload["passkey_wraps"][0]["prf_input"], wrap.prf_input)
        by_id = {entry["credential_id"]: entry for entry in payload["passkey_credentials"]}
        self.assertTrue(by_id[_b64url(bytes(wrapped_cred.credential_id))]["has_wrap"])
        self.assertFalse(by_id[_b64url(bytes(bare_cred.credential_id))]["has_wrap"])
        self.assertFalse(by_id[_b64url(bytes(bare_cred.credential_id))]["is_login_factor"])

    def test_stale_wraps_are_withheld(self) -> None:
        """Property 3, read side: a wrap for a dead keypair is never served."""
        profile = _profile()
        bundle = _enroll(profile)
        credential = _credential(profile)
        _wrap(bundle, credential)
        MessagingKeyBundle.objects.filter(pk=bundle.pk).update(version=bundle.version + 1)

        payload = _client_for(profile).get(reverse("e2ee.keys")).json()

        self.assertEqual(payload["passkey_wraps"], [])
        # ...and the roster reports no wrap, so the enrollment UI offers re-wrapping.
        by_id = {entry["credential_id"]: entry for entry in payload["passkey_credentials"]}
        self.assertFalse(by_id[_b64url(bytes(credential.credential_id))]["has_wrap"])


class ResetPurgesWrapsTests(TestCase):
    """Property 3, write side: a key reset destroys every wrap atomically."""

    def test_reset_deletes_wraps(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        _wrap(bundle, _credential(profile))

        response = _client_for(profile).post(
            reverse("e2ee.reset"),
            data=json.dumps(
                {
                    "confirm": "RESET",
                    "public_key": _b64(os.urandom(32)),
                    "recovery_wrapped_secret": _b64(os.urandom(72)),
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(E2EEPasskeyWrap.objects.exists())


class CredentialPromptTests(TestCase):
    """The post-login prompt: persistent, snoozeable, and satisfied by a real unlock path."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion

    def _post_login(self, profile: Profile):
        profile.welcome_onboarding_complete = True
        profile.save(update_fields=["welcome_onboarding_complete"])
        return _client_for(profile).get(reverse("post_login"))

    def test_passwordless_account_without_passkeys_is_prompted(self) -> None:
        profile = _profile()

        response = self._post_login(profile)

        self.assertEqual(response.url, reverse("account.set_password"))

    def test_snooze_is_persistent_not_per_session(self) -> None:
        profile = _profile()
        client = _client_for(profile)
        client.post(reverse("account.set_password.skip"))

        # A completely fresh session - the old per-session flag would re-prompt here.
        response = self._post_login(profile)

        self.assertNotEqual(response.url, reverse("account.set_password"))
        profile.refresh_from_db()
        self.assertIsNotNone(profile.credential_prompt_snoozed_until)
        self.assertGreater(profile.credential_prompt_snoozed_until, timezone.now() + timedelta(days=29))

    def test_expired_snooze_prompts_again(self) -> None:
        profile = _profile()
        Profile.objects.filter(pk=profile.pk).update(credential_prompt_snoozed_until=timezone.now() - timedelta(days=1))

        response = self._post_login(profile)

        self.assertEqual(response.url, reverse("account.set_password"))

    def test_a_passkey_satisfies_the_prompt_when_there_is_nothing_to_unlock(self) -> None:
        profile = _profile()
        _credential(profile, login_factor=False)

        response = self._post_login(profile)

        self.assertNotEqual(response.url, reverse("account.set_password"))

    def test_a_wrapped_passkey_satisfies_the_prompt(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        _wrap(bundle, _credential(profile, login_factor=False))

        response = self._post_login(profile)

        self.assertNotEqual(response.url, reverse("account.set_password"))

    def test_a_passkey_that_unwraps_nothing_still_prompts(self) -> None:
        """Owning a credential is not the same as owning a way back in.

        An authenticator without PRF support - or one enrolled before unlock
        wraps existed - leaves the account with encrypted messages it cannot
        reach on a new device. Counting it as "handled" silenced the prompt for
        exactly the accounts it exists to reach.
        """
        profile = _profile()
        _enroll(profile)
        _credential(profile, login_factor=True)

        response = self._post_login(profile)

        self.assertEqual(response.url, reverse("account.set_password"))

    def test_a_wrap_left_behind_by_a_reset_does_not_satisfy_the_prompt(self) -> None:
        profile = _profile()
        bundle = _enroll(profile)
        bundle.version = 2
        bundle.save(update_fields=["version"])
        _wrap(bundle, _credential(profile, login_factor=False), version=1)

        response = self._post_login(profile)

        self.assertEqual(response.url, reverse("account.set_password"))

    def test_a_password_satisfies_the_prompt(self) -> None:
        profile = _profile(password=PASSWORD)

        response = self._post_login(profile)

        self.assertNotEqual(response.url, reverse("account.set_password"))

    def test_the_snooze_refuses_a_get(self) -> None:
        """The snooze outlives the session, so a cross-site navigation must not set it."""
        profile = _profile()

        response = _client_for(profile).get(reverse("account.set_password.skip"))

        self.assertEqual(response.status_code, 405)
        profile.refresh_from_db()
        self.assertIsNone(profile.credential_prompt_snoozed_until)
