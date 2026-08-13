"""Tests for the E2EE endpoints' conversion to dual (session-or-credential) auth.

The endpoints in ``controllers.e2ee`` used to be plain Django ``View``s behind
``LoginRequiredMixin``. They are now DRF ``APIView``s so an OAuth2-consented
mobile client can reach the same URLs the web client uses.

What these tests pin down:

- The web client's existing session access still works (the conversion must be
  invisible to it).
- An OAuth2 token bearing the right scope now works too.
- A PAT-style ``ApiKey`` never works, even when its ``scopes`` list names the
  messaging scopes - ``permissions.OAUTH2_ONLY_SCOPES`` is enforced by
  credential *kind*, and this is the end-to-end proof of that.
- ``current_password`` is still demanded on the three key-replacing endpoints
  **under credential auth**. This is the critical regression guard: without it
  a stolen ``messages:write`` token could re-key a victim's account and lock
  them out of their own history.
- ``E2EEChangePasswordView`` was deliberately NOT converted.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import os

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.testcase import TestCase
from urbanlens.core.tests.oauth import first_party_application
from urbanlens.dashboard.controllers import e2ee as e2ee_controllers
from urbanlens.dashboard.external_api.mixins import DualAuthJsonView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

AccessToken = get_access_token_model()

CURRENT_PASSWORD = "correct-horse-battery-staple"


def _bearer(raw: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _profile_with_password(password: str = CURRENT_PASSWORD) -> Profile:
    user = baker.make(User)
    user.set_password(password)
    user.save(update_fields=["password"])
    return Profile.objects.get(user=user)


def _token_for(user: User, scope: str) -> str:
    token = AccessToken.objects.create(
        user=user,
        application=first_party_application(),
        token=f"tok-{os.urandom(8).hex()}",
        expires=timezone.now() + timedelta(hours=1),
        scope=scope,
    )
    return token.token


def _enroll(profile: Profile) -> MessagingKeyBundle:
    return MessagingKeyBundle.objects.create(
        profile=profile,
        public_key=_b64(os.urandom(32)),
        recovery_wrapped_secret=_b64(os.urandom(72)),
    )


class OwnKeysDualAuthTests(TestCase):
    """The read endpoint answers to both a session and an OAuth2 token."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.profile = _profile_with_password()
        _enroll(self.profile)
        self.url = reverse("e2ee.keys")

    def test_session_access_still_works(self) -> None:
        """The conversion must be invisible to the existing web client."""
        client = Client()
        client.force_login(self.profile.user)
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enrolled"])

    def test_oauth2_token_with_messages_read_works(self) -> None:
        token = _token_for(self.profile.user, ApiKeyScope.MESSAGES_READ.value)
        response = self.client.get(self.url, **_bearer(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enrolled"])

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_oauth2_token_without_the_scope_is_refused(self) -> None:
        token = _token_for(self.profile.user, ApiKeyScope.PINS_READ.value)
        self.assertEqual(self.client.get(self.url, **_bearer(token)).status_code, 403)

    def test_pat_api_key_is_refused_even_holding_the_scope(self) -> None:
        """OAUTH2_ONLY_SCOPES is about the credential *kind*, end to end.

        A bearer API key tends to end up in CI configs and screenshots; it must
        never be a route into someone's messages, even if something wrote the
        messaging scopes onto it.
        """
        key, raw = generate_api_key(self.profile.user, "leaky-ci-key")
        ApiKey.objects.filter(pk=key.pk).update(scopes=[ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.MESSAGES_WRITE.value])
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 403)


class ScopeSeparationTests(TestCase):
    """A read-only grant cannot reach a write endpoint."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = _profile_with_password()

    def test_read_only_token_cannot_enroll(self) -> None:
        token = _token_for(self.profile.user, ApiKeyScope.MESSAGES_READ.value)
        response = self.client.post(
            reverse("e2ee.enroll"),
            data=json.dumps({"public_key": _b64(os.urandom(32))}),
            content_type="application/json",
            **_bearer(token),
        )
        self.assertEqual(response.status_code, 403)


class CurrentPasswordProofUnderCredentialAuthTests(TestCase):
    """The three key-replacing endpoints still demand the account password.

    The whole point of the proof under credential auth: an OAuth2 token grants
    "send and read messages", not "replace this account's key material". A
    stolen token must not be sufficient on its own to re-key the account.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = _profile_with_password()
        self.token = _token_for(self.profile.user, f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}")

    def _post(self, url_name: str, payload: dict):
        return self.client.post(
            reverse(url_name),
            data=json.dumps(payload),
            content_type="application/json",
            **_bearer(self.token),
        )

    def _enroll_payload(self, **overrides) -> dict:
        payload = {
            "public_key": _b64(os.urandom(32)),
            "recovery_wrapped_secret": _b64(os.urandom(72)),
            "kdf_opslimit": 2,
            "kdf_memlimit": 67108864,
        }
        payload.update(overrides)
        return payload

    def test_enroll_rejects_a_wrong_current_password(self) -> None:
        response = self._post("e2ee.enroll", self._enroll_payload(current_password="wrong"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MessagingKeyBundle.objects.for_profile(self.profile).exists())

    def test_enroll_rejects_an_absent_current_password(self) -> None:
        response = self._post("e2ee.enroll", self._enroll_payload())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MessagingKeyBundle.objects.for_profile(self.profile).exists())

    def test_enroll_succeeds_with_the_right_current_password(self) -> None:
        response = self._post("e2ee.enroll", self._enroll_payload(current_password=CURRENT_PASSWORD))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(MessagingKeyBundle.objects.for_profile(self.profile).exists())

    def test_rewrap_rejects_a_wrong_current_password(self) -> None:
        _enroll(self.profile)
        response = self._post(
            "e2ee.rewrap",
            {"password_wrapped_secret": _b64(os.urandom(72)), "password_wrap_salt": _b64(os.urandom(16)), "current_password": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_rewrap_rejects_an_absent_current_password(self) -> None:
        _enroll(self.profile)
        response = self._post(
            "e2ee.rewrap",
            {"password_wrapped_secret": _b64(os.urandom(72)), "password_wrap_salt": _b64(os.urandom(16))},
        )
        self.assertEqual(response.status_code, 403)

    def test_reset_rejects_a_wrong_current_password(self) -> None:
        bundle = _enroll(self.profile)
        response = self._post(
            "e2ee.reset",
            {
                "confirm": "RESET",
                "public_key": _b64(os.urandom(32)),
                "recovery_wrapped_secret": _b64(os.urandom(72)),
                "current_password": "wrong",
            },
        )
        self.assertEqual(response.status_code, 403)
        bundle.refresh_from_db()
        self.assertEqual(bundle.version, 1)

    def test_reset_rejects_an_absent_current_password(self) -> None:
        bundle = _enroll(self.profile)
        response = self._post(
            "e2ee.reset",
            {"confirm": "RESET", "public_key": _b64(os.urandom(32)), "recovery_wrapped_secret": _b64(os.urandom(72))},
        )
        self.assertEqual(response.status_code, 403)
        bundle.refresh_from_db()
        self.assertEqual(bundle.version, 1)


class ErrorBodyShapeTests(TestCase):
    """Validation failures answer with the package's ``{"error": ...}`` JSON."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = _profile_with_password()
        self.token = _token_for(self.profile.user, ApiKeyScope.MESSAGES_WRITE.value)

    def test_malformed_blob_returns_a_json_error_body(self) -> None:
        """Previously an ``HttpResponseBadRequest`` with a text/plain body."""
        response = self.client.post(
            reverse("e2ee.enroll"),
            data=json.dumps({"public_key": "not-base64!!", "recovery_wrapped_secret": _b64(os.urandom(72))}),
            content_type="application/json",
            **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(response.json()["error"], "Invalid public_key")

    def test_malformed_json_from_a_session_keeps_the_error_body(self) -> None:
        """The web client's existing contract: 400 with ``{"error": ...}``."""
        client = Client()
        client.force_login(self.profile.user)
        response = client.post(reverse("e2ee.enroll"), data="{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Malformed JSON body")

    def test_malformed_json_from_a_credential_is_still_a_json_400(self) -> None:
        """Credential callers get the package's ``{"error": ...}`` here too.

        django-oauth-toolkit's authenticator reads the request body while
        verifying the token, so a malformed body raises DRF's ParseError
        during authentication - before any handler in this module runs and
        could substitute its own error shape. This used to be the one path
        that escaped as DRF's ``{"detail": ...}``; now that
        ``DualAuthJsonView`` inherits ``errors.ErrorEnvelopeMixin``, the
        view-level exception handler catches it on the way out and the caller
        sees the same envelope as every other failure. Worth asserting rather
        than assuming: authentication-time exceptions are raised outside the
        handler methods, which is exactly where a per-view override is easiest
        to get wrong.
        """
        response = self.client.post(
            reverse("e2ee.enroll"),
            data="{not json",
            content_type="application/json",
            **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertIn("error", payload)
        self.assertNotIn("detail", payload)


class GroupKeyTokenContractTests(TestCase):
    """The rotation payload must be keyed by the opaque per-member tokens."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.creator = _profile_with_password()
        self.member = _profile_with_password()
        for profile in (self.creator, self.member):
            _enroll(profile)
        Profile.objects.filter(pk__in=[self.creator.pk, self.member.pk]).update(direct_message_visibility="anyone")
        self.creator.refresh_from_db()
        self.member.refresh_from_db()

        from urbanlens.dashboard.services.messaging.group_chats import create_group_chat

        self.group = create_group_chat(self.creator, "Trip crew", [self.member])
        self.token = _token_for(self.creator.user, f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}")
        self.url = reverse("e2ee.group_key", kwargs={"group_uuid": self.group.uuid})

    def test_get_issues_opaque_member_ids_not_slugs(self) -> None:
        payload = self.client.get(self.url, **_bearer(self.token)).json()
        ids = {member["id"] for member in payload["members"]}
        slugs = {self.creator.slug, self.member.slug}
        self.assertTrue(ids.isdisjoint(slugs), "member ids must not be profile slugs")

    def test_slug_keyed_payload_is_rejected_with_409(self) -> None:
        """A stale client keying by slug gets the membership-mismatch 409."""
        wrapped = {
            (self.creator.slug or ""): _b64(os.urandom(48)),
            (self.member.slug or ""): _b64(os.urandom(48)),
        }
        response = self.client.post(
            self.url,
            data=json.dumps({"version": 1, "wrapped": wrapped}),
            content_type="application/json",
            **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 409)

    def test_token_keyed_payload_is_accepted(self) -> None:
        """The same request keyed correctly round-trips the GET's ids."""
        members = self.client.get(self.url, **_bearer(self.token)).json()["members"]
        wrapped = {member["id"]: _b64(os.urandom(48)) for member in members}
        response = self.client.post(
            self.url,
            data=json.dumps({"version": 1, "wrapped": wrapped}),
            content_type="application/json",
            **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["version"], 1)


class ChangePasswordStaysSessionOnlyTests(TestCase):
    """``E2EEChangePasswordView`` must never accept a credential.

    It calls ``user.set_password()`` on the *login* credential. Reaching it
    with a scoped messaging token would turn the compromise of one narrow,
    revocable token into permanent account takeover, so it is deliberately the
    one endpoint in the module left as a session-only Django view.
    """

    def test_it_is_not_a_dual_auth_view(self) -> None:
        self.assertFalse(issubclass(e2ee_controllers.E2EEChangePasswordView, DualAuthJsonView))

    def test_it_declares_no_credential_authenticators(self) -> None:
        """A plain Django View ignores these attributes - it must not have them."""
        self.assertFalse(hasattr(e2ee_controllers.E2EEChangePasswordView, "authentication_classes"))

    def test_a_messages_write_token_cannot_reach_it(self) -> None:
        baker.make(User)
        profile = _profile_with_password()
        token = _token_for(profile.user, ApiKeyScope.MESSAGES_WRITE.value)
        response = self.client.post(
            reverse("e2ee.change_password"),
            data=json.dumps({"new_auth_key": _b64(os.urandom(48)), "new_auth_salt": _b64(os.urandom(16))}),
            content_type="application/json",
            **_bearer(token),
        )
        # LoginRequiredMixin redirects an unauthenticated request to the login
        # page; either way the password is untouched.
        self.assertIn(response.status_code, (302, 401, 403))
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.check_password(CURRENT_PASSWORD))


class ConvertedViewInventoryTests(TestCase):
    """Every E2EE view except change-password is dual-auth, and each declares scopes."""

    EXPECTED_DUAL_AUTH = (
        "E2EEEnrollView",
        "E2EEOwnKeysView",
        "E2EEPartnerKeyView",
        "E2EEConversationKeyView",
        "E2EERewrapView",
        "E2EEGroupKeyView",
        "E2EERewrapAllView",
        "E2EEResetView",
    )

    def test_expected_views_are_dual_auth(self) -> None:
        for name in self.EXPECTED_DUAL_AUTH:
            with self.subTest(view=name):
                self.assertTrue(issubclass(getattr(e2ee_controllers, name), DualAuthJsonView))

    def test_every_dual_auth_view_declares_scopes_for_its_methods(self) -> None:
        """HasApiKeyScope fails closed, so a missing declaration is a dead endpoint."""
        for name in self.EXPECTED_DUAL_AUTH:
            view = getattr(e2ee_controllers, name)
            declared = set(view.required_scopes_by_method)
            implemented = {method.upper() for method in ("get", "post", "patch", "delete") if hasattr(view, method)}
            with self.subTest(view=name):
                self.assertEqual(implemented, declared)
