"""Tests for email normalization, cross-account lookup, and duplicate detection."""

from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.services.email_normalization import (
    find_user_by_email,
    is_email_taken,
    normalize_email,
)


class NormalizeEmailTests(TestCase):
    """Gmail dot/plus stripping and case-insensitivity."""

    def test_lowercases_and_strips_whitespace(self) -> None:
        self.assertEqual(normalize_email("  Jane@Example.com  "), "jane@example.com")

    def test_gmail_strips_dots_in_local_part(self) -> None:
        self.assertEqual(normalize_email("jake.smith@gmail.com"), "jakesmith@gmail.com")

    def test_gmail_strips_plus_suffix(self) -> None:
        self.assertEqual(normalize_email("jakesmith+spam@gmail.com"), "jakesmith@gmail.com")

    def test_gmail_strips_both_dots_and_plus(self) -> None:
        self.assertEqual(normalize_email("Jake.Smith+spam@gmail.com"), "jakesmith@gmail.com")

    def test_googlemail_alias_domain_also_normalized(self) -> None:
        self.assertEqual(normalize_email("jake.smith+x@googlemail.com"), "jakesmith@googlemail.com")

    def test_non_gmail_domain_keeps_dots_and_plus(self) -> None:
        self.assertEqual(normalize_email("Jake.Smith+spam@example.com"), "jake.smith+spam@example.com")

    def test_idempotent(self) -> None:
        once = normalize_email("Jake.Smith+spam@gmail.com")
        twice = normalize_email(once)
        self.assertEqual(once, twice)


class FindUserByEmailTests(TestCase):
    """Lookup matches primary and verified secondary emails, normalized."""

    def test_matches_primary_email_case_insensitive(self) -> None:
        user = baker.make(User, email="Jane@Example.com", is_active=True)
        self.assertEqual(find_user_by_email("jane@example.com"), user)

    def test_matches_gmail_dot_plus_variant_of_primary_email(self) -> None:
        user = baker.make(User, email="jakesmith@gmail.com", is_active=True)
        self.assertEqual(find_user_by_email("Jake.Smith+spam@gmail.com"), user)

    def test_does_not_match_inactive_user_by_default(self) -> None:
        baker.make(User, email="pending@example.com", is_active=False)
        self.assertIsNone(find_user_by_email("pending@example.com"))

    def test_active_only_false_matches_inactive_user(self) -> None:
        user = baker.make(User, email="pending@example.com", is_active=False)
        self.assertEqual(find_user_by_email("pending@example.com", active_only=False), user)

    def test_matches_verified_secondary_email(self) -> None:
        user = baker.make(User, email="main@example.com", is_active=True)
        ProfileEmail.objects.create(profile=user.profile, email="alt@example.com", is_verified=True)
        self.assertEqual(find_user_by_email("alt@example.com"), user)

    def test_ignores_unverified_secondary_email(self) -> None:
        user = baker.make(User, email="main@example.com", is_active=True)
        ProfileEmail.objects.create(profile=user.profile, email="alt@example.com", is_verified=False)
        self.assertIsNone(find_user_by_email("alt@example.com"))

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(find_user_by_email("nobody@example.com"))


class IsEmailTakenTests(TestCase):
    """Duplicate detection used by registration, contact settings, and secondary-email add."""

    def test_primary_email_is_taken(self) -> None:
        baker.make(User, email="jane@example.com")
        self.assertTrue(is_email_taken("jane@example.com"))

    def test_gmail_variant_of_primary_email_is_taken(self) -> None:
        baker.make(User, email="jakesmith@gmail.com")
        self.assertTrue(is_email_taken("jake.smith+newsletter@gmail.com"))

    def test_verified_secondary_email_is_taken(self) -> None:
        user = baker.make(User, email="main@example.com")
        ProfileEmail.objects.create(profile=user.profile, email="alt@example.com", is_verified=True)
        self.assertTrue(is_email_taken("alt@example.com"))

    def test_unverified_secondary_email_is_not_taken(self) -> None:
        user = baker.make(User, email="main@example.com")
        ProfileEmail.objects.create(profile=user.profile, email="alt@example.com", is_verified=False)
        self.assertFalse(is_email_taken("alt@example.com"))

    def test_exclude_user_id_allows_keeping_own_email(self) -> None:
        user = baker.make(User, email="jane@example.com")
        self.assertFalse(is_email_taken("jane@example.com", exclude_user_id=user.pk))

    def test_exclude_user_id_still_blocks_other_accounts(self) -> None:
        user = baker.make(User, email="jane@example.com")
        baker.make(User, email="other@example.com")
        self.assertTrue(is_email_taken("other@example.com", exclude_user_id=user.pk))

    def test_unused_email_is_not_taken(self) -> None:
        self.assertFalse(is_email_taken("nobody@example.com"))


class ProfileEmailUniqueVerifiedConstraintTests(TestCase):
    """Only one profile may hold a verified claim on a given normalized address."""

    def test_second_verified_claim_on_same_normalized_email_raises(self) -> None:
        from django.db import IntegrityError, transaction

        user_a = baker.make(User, email="a@example.com")
        user_b = baker.make(User, email="b@example.com")
        ProfileEmail.objects.create(profile=user_a.profile, email="shared@example.com", is_verified=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProfileEmail.objects.create(profile=user_b.profile, email="shared@example.com", is_verified=True)

    def test_two_unverified_claims_on_same_email_are_allowed(self) -> None:
        user_a = baker.make(User, email="a@example.com")
        user_b = baker.make(User, email="b@example.com")
        ProfileEmail.objects.create(profile=user_a.profile, email="shared@example.com", is_verified=False)
        ProfileEmail.objects.create(profile=user_b.profile, email="shared@example.com", is_verified=False)
        self.assertEqual(ProfileEmail.objects.filter(normalized_email="shared@example.com").count(), 2)


@settings(max_examples=50, deadline=None)
@given(
    local=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="."),
        min_size=1,
        max_size=20,
    ).filter(lambda s: not s.startswith(".") and not s.endswith(".") and ".." not in s),
)
def test_gmail_dot_stripping_is_idempotent(local: str) -> None:
    """Normalizing a Gmail address twice gives the same result as once."""
    email = f"{local}@gmail.com"
    once = normalize_email(email)
    twice = normalize_email(once)
    assert once == twice  # nosec B101
