"""Resetting a password offers to revoke API keys, and defaults to keeping them.

Resetting a password invalidates every session, which is what makes it the
standard answer to a suspected compromise. It does not touch ``ApiKey`` rows -
and ``ApiKeyCreateView`` mints a key behind ``LoginRequiredMixin`` alone, with
no current-password proof. So a session-only compromise (a stolen cookie, a
borrowed unlocked laptop) is enough to mint a long-lived credential, and the
victim's natural remedy does not remove it.

The offer has to happen on this POST, because ``post_reset_login`` is False:
this is the only moment in the flow where the account is identified.

Two properties matter more than the feature itself, and both are pinned here:

* **The default is to keep the keys.** Most resets are ordinary forgetfulness,
  and a key that stops working without the owner choosing that is a broken
  integration they have to debug.
* **The page must leak nothing.** Django resolves ``self.user`` from the uidb64
  *before* checking the token, and a uidb64 is an encoded integer primary key -
  so anything rendered from ``self.user`` alone is readable for any account by
  anyone who can count.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey
from urbanlens.dashboard.services.auth.api_keys import authenticate_api_key, generate_api_key

NEW_PASSWORD = "A Fresh Correct Horse Battery Staple 42!"  # nosec B105 - test fixture password

#: The breach check reaches Have I Been Pwned, which the suite's network guard
#: refuses. Patched to "not breached" so these tests exercise the reset, not the
#: validator - same idiom as test_password_validators/test_onboarding_flow.
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"


class PasswordResetApiKeyRevocationTests(TestCase):
    """The choice, its default, and what the page is allowed to say."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(_HIBP_PATCH, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = baker.make(User, email="owner@example.com", is_active=True)
        self.user.set_password("The Old Password 17!")  # nosec B106 - test fixture password
        self.user.save(update_fields=["password"])
        _key, self.raw_key = generate_api_key(self.user, name="my-integration")

    def _start_reset(self, user: User | None = None) -> str:
        """Walk the emailed link so the session carries the internal reset token.

        Args:
            user: Whose link to build. Defaults to the fixture user.

        Returns:
            The URL to POST the new password to.
        """
        user = user or self.user
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        self.client.get(reverse("password_reset_confirm", args=[uidb64, token]), follow=True)
        return reverse("password_reset_confirm", args=[uidb64, "set-password"])

    def _submit(self, url: str, **extra: str):
        return self.client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD, **extra})

    # -- the default ---------------------------------------------------------

    def test_a_reset_without_the_choice_leaves_every_key_working(self) -> None:
        """The default, and the assertion that matters most."""
        response = self._submit(self._start_reset())

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertIsNotNone(authenticate_api_key(self.raw_key))

    def test_choosing_to_revoke_stops_every_active_key(self) -> None:
        self._submit(self._start_reset(), revoke_api_keys="1")

        self.assertIsNone(authenticate_api_key(self.raw_key))

    def test_the_password_still_changes_when_revocation_is_chosen(self) -> None:
        """Both effects, in one request - neither may cost the other."""
        self._submit(self._start_reset(), revoke_api_keys="1")

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertIsNone(authenticate_api_key(self.raw_key))

    def test_revocation_is_scoped_to_the_resetting_account(self) -> None:
        stranger = baker.make(User, email="stranger@example.com", is_active=True)
        _key, stranger_raw = generate_api_key(stranger, name="theirs")

        self._submit(self._start_reset(), revoke_api_keys="1")

        self.assertIsNotNone(authenticate_api_key(stranger_raw))

    def test_a_rejected_password_revokes_nothing(self) -> None:
        """The form has to be valid before anything destructive happens."""
        url = self._start_reset()

        self.client.post(
            url, {"new_password1": NEW_PASSWORD, "new_password2": "something else", "revoke_api_keys": "1"}
        )

        self.assertIsNotNone(authenticate_api_key(self.raw_key))

    def test_an_already_revoked_key_keeps_its_original_timestamp(self) -> None:
        """``revoked_at`` records when a key stopped working, not when this ran."""
        key = ApiKey.objects.get(user=self.user)
        earlier = timezone.now() - datetime.timedelta(days=3)
        ApiKey.objects.filter(pk=key.pk).update(revoked_at=earlier)

        self._submit(self._start_reset(), revoke_api_keys="1")

        key.refresh_from_db()
        self.assertEqual(key.revoked_at, earlier)

    # -- what the page offers, and to whom -----------------------------------

    def test_the_prompt_is_absent_when_there_are_no_keys(self) -> None:
        """The owner's rule: do not ask somebody about keys they do not have."""
        keyless = baker.make(User, email="keyless@example.com", is_active=True)
        keyless.set_password("The Old Password 17!")  # nosec B106 - test fixture password
        keyless.save(update_fields=["password"])

        uidb64 = urlsafe_base64_encode(force_bytes(keyless.pk))
        response = self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(keyless)]), follow=True
        )

        self.assertEqual(response.context["active_api_key_count"], 0)
        self.assertNotContains(response, "api-key-choice-dialog")

    def test_the_prompt_is_present_when_there_is_a_key(self) -> None:
        """Negative-then-positive: the assertion above must be able to fail."""
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        response = self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )

        self.assertEqual(response.context["active_api_key_count"], 1)
        self.assertContains(response, "api-key-choice-dialog")

    def test_an_expired_link_for_a_real_account_reveals_nothing(self) -> None:
        """The disclosure this page must not become.

        ``self.user`` is resolved from the uidb64 before the token is checked,
        and a uidb64 is an encoded integer pk. Keying the count on ``self.user``
        rather than on ``validlink`` would publish any account's key state to
        anyone willing to count.
        """
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))

        response = self.client.get(reverse("password_reset_confirm", args=[uidb64, "bad-token"]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["validlink"])
        self.assertEqual(response.context["active_api_key_count"], 0)
        self.assertNotContains(response, "api-key-choice-dialog")
        self.assertNotContains(
            response, "my-integration", msg_prefix="a key's name is user-authored text and must never reach this page"
        )

    def test_the_key_name_is_never_rendered_even_on_a_valid_link(self) -> None:
        """The count is enough to phrase the question; the names are not needed."""
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))

        response = self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )

        self.assertNotContains(response, "my-integration")

    def test_the_hidden_field_defaults_to_keeping_the_keys(self) -> None:
        """No JavaScript, or JavaScript that throws, must not revoke anything."""
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        response = self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )

        self.assertContains(response, 'name="revoke_api_keys" id="revoke-api-keys-field" value=""')
