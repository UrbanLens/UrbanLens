"""The sign-in pages configure the E2EE client from the ``#e2ee-urls`` island, then wire their form from auth.js.

auth.js must load after e2ee.js and synchronously: the login form has to be wired before it can be submitted, or
the raw password goes to the server.
"""

from __future__ import annotations

import json
import re

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from model_bakery import baker

from urbanlens.dashboard.controllers.account import _WEBAUTHN_PENDING_USER_KEY
from urbanlens.dashboard.models.account import WebAuthnCredential

ISLAND = re.compile(r'<script id="e2ee-urls" type="application/json">(.*?)</script>', re.DOTALL)
SCRIPT_SRC = re.compile(r'<script\b[^>]*\bsrc="([^"]+)"')


class AuthPagesE2EEIslandTests(TestCase):
    def _assert_wired(self, html: str, **extra: str) -> None:
        islands = ISLAND.findall(html)
        self.assertEqual(len(islands), 1)
        urls = json.loads(islands[0])
        self.assertEqual(urls["loginParams"], reverse("e2ee.login_params"))
        self.assertEqual(urls["conversationKeyBase"], reverse("e2ee.conversation_key", args=["x"]).removesuffix("x/"))
        self.assertEqual(urls["login"], reverse("login"))
        for key, value in extra.items():
            self.assertEqual(urls[key], value)
        sources = [src.split("?")[0] for src in SCRIPT_SRC.findall(html)]
        e2ee = next(i for i, src in enumerate(sources) if src.endswith("/dashboard/js/e2ee.js"))
        auth = next(i for i, src in enumerate(sources) if src.endswith("/dashboard/js/auth.js"))
        self.assertLess(e2ee, auth)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>[^<]*UrbanLensE2EE")
        self.assertNotIn(" defer", html[html.index("/dashboard/js/auth.js") - 80 : html.index("/dashboard/js/auth.js")])

    def test_login(self) -> None:
        response = self.client.get(reverse("login"))
        self._assert_wired(response.content.decode())
        self.assertContains(response, 'id="password-login-form"')

    def test_signup(self) -> None:
        response = self.client.get(reverse("signup"))
        self._assert_wired(response.content.decode(), validatePassword=reverse("validate_password_policy"))
        self.assertContains(response, 'id="signup-form"')

    def test_password_reset_confirm_carries_the_accounts_mode(self) -> None:
        user = baker.make(User, email="reset@example.com", is_active=True)
        user.set_password("The Old Password 17!")  # nosec B106 - test fixture password
        user.save(update_fields=["password"])
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        response = self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(user)]), follow=True
        )
        self._assert_wired(response.content.decode(), validatePassword=reverse("validate_password_policy"))
        self.assertContains(response, 'data-e2ee-mode="legacy"')

    def test_set_password_offers_the_passkey_endpoints(self) -> None:
        user = baker.make(User, username="oauth-owl", is_active=True)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        self.client.force_login(user)
        response = self.client.get(reverse("account.set_password"))
        self._assert_wired(
            response.content.decode(),
            changePassword=reverse("e2ee.change_password"),
            passkeyWrap=reverse("e2ee.passkey_wrap"),
            passkeyRegisterOptions=reverse("settings.security.passkeys.options"),
            passkeyRegister=reverse("settings.security.passkeys.register"),
            passkeyBase=reverse("settings.security.passkeys.register"),
        )
        self.assertContains(response, f'data-self-slug="{response.context["self_slug"]}"')
        self.assertContains(response, f'data-done-url="{reverse("post_login")}"')

    def test_login_2fa_passes_the_ceremony_urls_to_the_retry_button(self) -> None:
        user = baker.make(User, username="passkey-owl", is_active=True)
        baker.make(WebAuthnCredential, user=user)
        session = self.client.session
        session[_WEBAUTHN_PENDING_USER_KEY] = user.pk
        session.save()
        response = self.client.get(reverse("login.2fa"))
        self._assert_wired(response.content.decode())
        sources = [src.split("?")[0] for src in SCRIPT_SRC.findall(response.content.decode())]
        self.assertLess(
            next(i for i, s in enumerate(sources) if s.endswith("/dashboard/js/webauthn.js")),
            next(i for i, s in enumerate(sources) if s.endswith("/dashboard/js/auth.js")),
        )
        self.assertContains(response, f'data-options-url="{reverse("login.2fa.options")}"')
        self.assertContains(response, f'data-verify-url="{reverse("login.2fa.verify")}"')
