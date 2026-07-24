"""Tests for password complexity, HIBP checks, and passphrase suggestions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.urls import reverse
from hypothesis import given, settings, strategies as st
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.passphrases import generate_passphrases
from urbanlens.dashboard.validators.password import ComplexityValidator, HaveIBeenPwnedValidator

_STRONG_PASSWORD = "Zebra-quilt-nexus-42!"
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"


class ComplexityValidatorTests(SimpleTestCase):
    """ComplexityValidator requires upper, lower, and digit-or-symbol."""

    def setUp(self) -> None:
        self.validator = ComplexityValidator()

    def test_accepts_upper_lower_and_digit(self) -> None:
        self.validator.validate("GoodPassword1")

    def test_accepts_upper_lower_and_symbol(self) -> None:
        self.validator.validate("GoodPassword!")

    def test_rejects_missing_uppercase(self) -> None:
        with pytest.raises(ValidationError) as ctx:
            self.validator.validate("goodpassword1")
        self.assertIn("uppercase", str(ctx.value).lower())

    def test_rejects_missing_lowercase(self) -> None:
        with pytest.raises(ValidationError) as ctx:
            self.validator.validate("GOODPASSWORD1")
        self.assertIn("lowercase", str(ctx.value).lower())

    def test_rejects_missing_digit_and_symbol(self) -> None:
        with pytest.raises(ValidationError) as ctx:
            self.validator.validate("GoodPassword")
        self.assertIn("digit", str(ctx.value).lower())

    def test_help_text_mentions_requirements(self) -> None:
        text = self.validator.get_help_text().lower()
        self.assertIn("uppercase", text)
        self.assertIn("lowercase", text)


class HaveIBeenPwnedValidatorTests(SimpleTestCase):
    """HaveIBeenPwnedValidator rejects breached passwords and fails open on errors."""

    def setUp(self) -> None:
        self.validator = HaveIBeenPwnedValidator()

    def test_rejects_pwned_password(self) -> None:
        with patch(_HIBP_PATCH, return_value=True), pytest.raises(ValidationError) as ctx:
            self.validator.validate("PwnedPassword1!")
        self.assertIn("breach", str(ctx.value).lower())
        self.assertTrue(any(getattr(err, "code", None) == "password_pwned" for err in ctx.value.error_list))

    def test_accepts_clean_password(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            self.validator.validate("UniqueFreshPassphrase9!")

    def test_fails_open_when_api_unavailable(self) -> None:
        with patch(_HIBP_PATCH, return_value=None):
            self.validator.validate("UniqueFreshPassphrase9!")


class HaveIBeenPwnedGatewayTests(SimpleTestCase):
    """Gateway parses the HIBP range response using k-anonymity."""

    def _gateway_with_mock_session(self):
        from urbanlens.dashboard.services.apis.security.hibp import HaveIBeenPwnedGateway

        gateway = HaveIBeenPwnedGateway()
        session = MagicMock()
        object.__setattr__(gateway, "session", session)
        return gateway, session

    def test_detects_matching_suffix(self) -> None:
        import hashlib

        candidate = "Password123!"
        digest = hashlib.sha1(candidate.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        suffix = digest[5:]

        gateway, session = self._gateway_with_mock_session()
        response = MagicMock()
        response.text = f"{suffix}:42\nABCDEF0123:1\n"
        response.raise_for_status = MagicMock()
        session.get.return_value = response

        self.assertTrue(gateway.is_password_pwned(candidate))
        called_url = session.get.call_args.args[0]
        self.assertIn(digest[:5], called_url)
        self.assertNotIn(digest, called_url)
        self.assertNotIn(candidate, called_url)

    def test_returns_false_when_not_listed(self) -> None:
        gateway, session = self._gateway_with_mock_session()
        response = MagicMock()
        response.text = "ABCDEF0123:1\n"
        response.raise_for_status = MagicMock()
        session.get.return_value = response

        self.assertFalse(gateway.is_password_pwned("TotallyUniquePassphrase42!"))

    def test_returns_none_on_network_error(self) -> None:
        gateway, session = self._gateway_with_mock_session()
        session.get.side_effect = ConnectionError("down")

        self.assertIsNone(gateway.is_password_pwned("AnythingGoes1!"))


class PassphraseGenerationTests(SimpleTestCase):
    """Generated passphrases meet complexity rules."""

    def test_returns_requested_count(self) -> None:
        phrases = generate_passphrases(5)
        self.assertEqual(len(phrases), 5)
        self.assertEqual(len(set(phrases)), 5)

    def test_each_phrase_passes_complexity(self) -> None:
        validator = ComplexityValidator()
        for phrase in generate_passphrases(8):
            validator.validate(phrase)
            self.assertGreaterEqual(len(phrase), 12)

    @given(st.integers(min_value=1, max_value=10))
    @settings(max_examples=20, deadline=None)
    def test_count_is_respected(self, count: int) -> None:
        phrases = generate_passphrases(count)
        self.assertEqual(len(phrases), count)


class SuggestPassphrasesViewTests(TestCase):
    """GET /accounts/suggest-passphrases/ returns five suggestions."""

    def test_returns_five_passphrases(self) -> None:
        response = self.client.get(reverse("suggest_passphrases"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("passphrases", payload)
        self.assertEqual(len(payload["passphrases"]), 5)
        for phrase in payload["passphrases"]:
            ComplexityValidator().validate(phrase)

    def test_rate_limit_returns_429(self) -> None:
        from urbanlens.dashboard.controllers import account as account_controller

        with patch.object(account_controller, "_PASSPHRASE_RATE_LIMIT", 2):
            self.assertEqual(self.client.get(reverse("suggest_passphrases")).status_code, 200)
            self.assertEqual(self.client.get(reverse("suggest_passphrases")).status_code, 200)
            self.assertEqual(self.client.get(reverse("suggest_passphrases")).status_code, 429)


class SignupPasswordValidationIntegrationTests(TestCase):
    """Signup form runs complexity + HIBP validators."""

    def test_signup_accepts_strong_password(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self.client.post(
                reverse("signup"),
                {
                    "username": "stronguser",
                    "email": "stronguser@example.com",
                    "password1": _STRONG_PASSWORD,
                    "password2": _STRONG_PASSWORD,
                },
            )
        self.assertEqual(response.status_code, 302)

    def test_signup_rejects_weak_password(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self.client.post(
                reverse("signup"),
                {
                    "username": "weakuser",
                    "email": "weakuser@example.com",
                    "password1": "alllowercase1",
                    "password2": "alllowercase1",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "uppercase")

    def test_signup_rejects_pwned_password(self) -> None:
        with patch(_HIBP_PATCH, return_value=True):
            response = self.client.post(
                reverse("signup"),
                {
                    "username": "pwneduser",
                    "email": "pwneduser@example.com",
                    "password1": _STRONG_PASSWORD,
                    "password2": _STRONG_PASSWORD,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "breach")

    def test_django_validate_password_uses_configured_validators(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            with pytest.raises(ValidationError):
                validate_password("short1A")
            validate_password("LongEnoughPassphrase9")


class ValidatePasswordPolicyViewTests(TestCase):
    """POST /accounts/validate-password/ - the E2EE flows' pre-derive policy check.

    The client derives the login credential before submit, so this endpoint is
    the only place the configured AUTH_PASSWORD_VALIDATORS ever see the real
    password (docs/PROBLEMS.md, decision 2026-07-23).
    """

    def setUp(self) -> None:
        super().setUp()
        # The per-IP rate key is shared cache state - every test in this class
        # posts from the same test-client IP, so without a reset the rate-limit
        # test inherits the hit count from whichever tests ran before it.
        from django.core.cache import cache

        from urbanlens.dashboard.controllers.account import _PASSWORD_CHECK_RATE_KEY

        cache.delete(_PASSWORD_CHECK_RATE_KEY.format(ip="127.0.0.1"))

    def _post(self, body: dict):
        import json as jsonlib

        return self.client.post(reverse("validate_password_policy"), jsonlib.dumps(body), content_type="application/json")

    def test_strong_password_is_valid(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": _STRONG_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"valid": True, "errors": []})

    def test_short_password_reports_length_error(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": "Ab1!"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["valid"])
        self.assertTrue(any("12" in message for message in payload["errors"]))

    def test_complexity_failure_is_reported(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": "alllowercaseonly!"})
        payload = response.json()
        self.assertFalse(payload["valid"])
        self.assertTrue(any("uppercase" in message.lower() for message in payload["errors"]))

    def test_pwned_password_is_rejected(self) -> None:
        with patch(_HIBP_PATCH, return_value=True):
            response = self._post({"password": _STRONG_PASSWORD})
        payload = response.json()
        self.assertFalse(payload["valid"])
        self.assertTrue(any("breach" in message.lower() for message in payload["errors"]))

    def test_username_similarity_is_checked(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": "Jessamyn-Barrows1", "username": "jessamyn-barrows1", "email": ""})
        payload = response.json()
        self.assertFalse(payload["valid"])

    def test_anonymous_access_is_allowed(self) -> None:
        """Signup has no session yet - the endpoint must not require login."""
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": _STRONG_PASSWORD})
        self.assertEqual(response.status_code, 200)

    def test_missing_password_is_400(self) -> None:
        self.assertEqual(self._post({}).status_code, 400)

    def test_malformed_body_is_400(self) -> None:
        response = self.client.post(reverse("validate_password_policy"), "not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_oversized_password_is_invalid_not_500(self) -> None:
        response = self._post({"password": "Aa1!" * 500})
        payload = response.json()
        self.assertFalse(payload["valid"])

    def test_rate_limit_returns_429(self) -> None:
        from urbanlens.dashboard.controllers import account as account_controller

        with patch(_HIBP_PATCH, return_value=False), patch.object(account_controller, "_PASSWORD_CHECK_RATE_LIMIT", 2):
            self.assertEqual(self._post({"password": _STRONG_PASSWORD}).status_code, 200)
            self.assertEqual(self._post({"password": _STRONG_PASSWORD}).status_code, 200)
            self.assertEqual(self._post({"password": _STRONG_PASSWORD}).status_code, 429)

    def test_password_never_appears_in_the_response(self) -> None:
        with patch(_HIBP_PATCH, return_value=False):
            response = self._post({"password": "SuperSecretValue77!"})
        self.assertNotIn("SuperSecretValue77!", response.content.decode())
