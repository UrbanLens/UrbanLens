"""Tests for SSO username generation in the social-auth pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.social_auth.pipeline import (
    _sanitize_sso_username,
    generate_sso_username,
)
from urbanlens.dashboard.services.username import USERNAME_RE


class SanitizeSsoUsernameTests(SimpleTestCase):
    """Provider handles are normalized to UrbanLens username rules."""

    def test_email_local_part_uses_prefix_before_at(self) -> None:
        self.assertEqual(_sanitize_sso_username("jane.doe@gmail.com"), "jane_doe")

    def test_discord_username_passes_through_when_valid(self) -> None:
        self.assertEqual(_sanitize_sso_username("urbex_explorer"), "urbex_explorer")

    def test_too_short_after_sanitization_returns_none(self) -> None:
        self.assertIsNone(_sanitize_sso_username("ab"))

    def test_truncates_to_thirty_characters(self) -> None:
        raw = "a" * 40
        sanitized = _sanitize_sso_username(raw)
        self.assertIsNotNone(sanitized)
        assert sanitized is not None  # nosec B101
        self.assertLessEqual(len(sanitized), 30)


class GenerateSsoUsernameTests(TestCase):
    """Pipeline step prefers provider handles when they are available."""

    def test_returning_user_keeps_existing_username(self) -> None:
        user = baker.make(User, username="existing_user")
        backend = SimpleNamespace(name="discord")
        result = generate_sso_username(
            backend,
            user,
            {"username": "other_name"},
            {},
        )
        self.assertEqual(result, {"username": "existing_user"})

    def test_discord_username_used_when_available(self) -> None:
        backend = SimpleNamespace(name="discord")
        result = generate_sso_username(
            backend,
            None,
            {"username": "discord_handle"},
            {},
        )
        self.assertEqual(result, {"username": "discord_handle"})

    def test_google_email_prefix_used_when_available(self) -> None:
        backend = SimpleNamespace(name="google-oauth2")
        result = generate_sso_username(
            backend,
            None,
            {},
            {"email": "photo.explorer@gmail.com"},
        )
        self.assertEqual(result, {"username": "photo_explorer"})

    def test_taken_provider_username_falls_back_to_random(self) -> None:
        baker.make(User, username="taken_name")
        backend = SimpleNamespace(name="discord")
        with patch(
            "urbanlens.dashboard.services.username.UsernameGenerator.generate",
            return_value="randomfallback",
        ) as random_username:
            result = generate_sso_username(
                backend,
                None,
                {"username": "taken_name"},
                {},
            )
        random_username.assert_called_once_with()
        self.assertEqual(result, {"username": "randomfallback"})


@settings(max_examples=50, deadline=None)
@given(
    local=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_."),
        min_size=3,
        max_size=20,
    ),
    domain=st.sampled_from(["gmail.com", "example.org", "urbanlens.dev"]),
)
def test_sanitize_email_local_part_is_valid_or_none(local: str, domain: str) -> None:
    """Sanitized email prefixes always satisfy username rules when present."""
    sanitized = _sanitize_sso_username(f"{local}@{domain}")
    if sanitized is not None:
        assert USERNAME_RE.match(sanitized)  # nosec B101
