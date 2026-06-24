"""Tests for username normalization and collision detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.social_auth.pipeline import generate_sso_username
from urbanlens.dashboard.services.username import (
    USERNAME_RE,
    UsernameGenerator,
    normalize_username_key,
    username_is_taken,
)


class NormalizeUsernameKeyTests(TestCase):
    """Confusable and case variants normalize to the same key."""

    def test_case_insensitive(self) -> None:
        self.assertEqual(normalize_username_key("Explorer"), normalize_username_key("explorer"))

    def test_o_and_zero_are_equivalent(self) -> None:
        self.assertEqual(normalize_username_key("john"), normalize_username_key("j0hn"))

    def test_l_one_and_i_are_equivalent(self) -> None:
        self.assertEqual(normalize_username_key("bill"), normalize_username_key("bi11"))
        self.assertEqual(normalize_username_key("bill"), normalize_username_key("biil"))

    def test_underscore_variants_are_equivalent(self) -> None:
        self.assertEqual(normalize_username_key("JohnM"), normalize_username_key("John_M"))
        self.assertEqual(normalize_username_key("JohnM"), normalize_username_key("JohnM_"))
        self.assertEqual(normalize_username_key("JohnM"), normalize_username_key("_J_o_h_n_M_"))

    def test_common_leet_substitutions(self) -> None:
        pairs = (
            ("alpha", "4lpha"),   # a=4
            ("elite", "e1ite"),   # l=1
            ("gates", "g4tes"),   # a=4
            ("blaze", "8laze"),   # b=8
            ("trend", "7rend"),   # t=7
            ("snooze", "snooz3"), # e=3
            ("pizza", "p1zza"),   # i=1
        )
        for plain, variant in pairs:
            with self.subTest(plain=plain, variant=variant):
                self.assertEqual(normalize_username_key(plain), normalize_username_key(variant))


class UsernameIsTakenTests(TestCase):
    """Collision detection blocks confusable duplicates."""

    def test_exact_match_is_taken(self) -> None:
        baker.make(User, username="urbanlens")
        self.assertTrue(username_is_taken("urbanlens"))

    def test_case_variant_is_taken(self) -> None:
        baker.make(User, username="urbanlens")
        self.assertTrue(username_is_taken("UrbanLens"))

    def test_confusable_variant_is_taken(self) -> None:
        baker.make(User, username="john")
        self.assertTrue(username_is_taken("j0hn"))

    def test_exclude_user_allows_keeping_own_username(self) -> None:
        user = baker.make(User, username="john")
        self.assertFalse(username_is_taken("john", exclude_user_id=user.pk))
        self.assertFalse(username_is_taken("j0hn", exclude_user_id=user.pk))

    def test_exclude_user_still_blocks_other_accounts(self) -> None:
        user = baker.make(User, username="john")
        baker.make(User, username="jane")
        self.assertTrue(username_is_taken("jane", exclude_user_id=user.pk))


class UsernameGeneratorTests(TestCase):
    """UsernameGenerator produces valid, distinct usernames."""

    def test_generate_matches_username_re(self) -> None:
        username = UsernameGenerator.generate()
        self.assertIsNotNone(USERNAME_RE.match(username))

    def test_generate_produces_distinct_results(self) -> None:
        names = {UsernameGenerator.generate() for _ in range(10)}
        self.assertGreater(len(names), 1)


class GenerateSsoUsernameConfusableTests(TestCase):
    """SSO signup avoids provider handles that collide confusably."""

    def test_confusable_provider_username_falls_back_to_random(self) -> None:
        baker.make(User, username="john")
        backend = SimpleNamespace(name="discord")
        with patch(
            "urbanlens.dashboard.services.username.UsernameGenerator.generate",
            return_value="randomfallback",
        ) as random_username:
            result = generate_sso_username(
                backend,
                None,
                {"username": "j0hn"},
                {},
            )
        random_username.assert_called_once_with()
        self.assertEqual(result, {"username": "randomfallback"})


@settings(max_examples=50, deadline=None)
@given(
    left=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_"),
        min_size=3,
        max_size=20,
    ),
)
def test_normalize_username_key_is_idempotent(left: str) -> None:
    """Normalization is stable when applied repeatedly."""
    once = normalize_username_key(left)
    twice = normalize_username_key(once)
    assert once == twice
    if USERNAME_RE.match(left):
        assert normalize_username_key(left.casefold()) == once
