"""Tests for services.api_keys: generation, hash-based verification, and revocation.

Mirrors the coverage shape of backup codes (test_backup_services.py doesn't
cover BackupCode itself, so this is closer to two_factor's own
verify_and_consume_backup_code tests in spirit): plaintext is only ever
returned at generation time, every later check goes through a salted hash,
and revocation is immediate and scoped to the owning user.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope, ApiKeyUsageLog
from urbanlens.dashboard.services.api_keys import (
    KEY_LABEL,
    USAGE_LOG_LIMIT,
    authenticate_api_key,
    generate_api_key,
    record_api_key_usage,
    revoke_api_key,
)


class GenerateApiKeyTests(TestCase):
    """generate_api_key issues a usable, uniquely-prefixed, hash-backed key."""

    def test_raw_key_is_shaped_like_label_then_prefix_then_secret(self) -> None:
        """The prefix/secret boundary is fixed-length, not delimiter-based - see
        authenticate_api_key's docstring for why (token_urlsafe's alphabet
        includes "_", so a delimiter split could misparse a lucky prefix)."""
        user = baker.make(User)
        api_key, raw_key = generate_api_key(user, "Zapier")
        self.assertTrue(raw_key.startswith(f"{KEY_LABEL}_"))
        remainder = raw_key[len(f"{KEY_LABEL}_") :]
        prefix, secret = remainder[: len(api_key.prefix)], remainder[len(api_key.prefix) :]
        self.assertEqual(prefix, api_key.prefix)
        self.assertTrue(secret)

    def test_plaintext_secret_is_never_persisted(self) -> None:
        user = baker.make(User)
        api_key, raw_key = generate_api_key(user, "Zapier")
        secret = raw_key[len(f"{KEY_LABEL}_") + len(api_key.prefix) :]
        self.assertNotIn(secret, api_key.key_hash)
        self.assertNotEqual(api_key.key_hash, secret)

    def test_default_scopes_grant_profile_read_and_pins_write(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        self.assertCountEqual(api_key.scopes, [ApiKeyScope.PROFILE_READ.value, ApiKeyScope.PINS_WRITE.value])

    def test_blank_name_falls_back_to_default_label(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "   ")
        self.assertEqual(api_key.name, "API Key")

    def test_name_is_trimmed_and_truncated(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "  My App  " + "x" * 200)
        self.assertTrue(api_key.name.startswith("My App"))
        self.assertLessEqual(len(api_key.name), 100)

    def test_two_keys_for_the_same_user_get_distinct_prefixes(self) -> None:
        user = baker.make(User)
        first, _ = generate_api_key(user, "One")
        second, _ = generate_api_key(user, "Two")
        self.assertNotEqual(first.prefix, second.prefix)


class AuthenticateApiKeyTests(TestCase):
    """authenticate_api_key resolves a raw key to its row only when it's valid and active."""

    def test_correct_key_authenticates(self) -> None:
        user = baker.make(User)
        api_key, raw_key = generate_api_key(user, "Zapier")
        resolved = authenticate_api_key(raw_key)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, api_key.pk)

    def test_wrong_secret_with_correct_prefix_is_rejected(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        tampered = f"{KEY_LABEL}_{api_key.prefix}not-the-real-secret"
        self.assertIsNone(authenticate_api_key(tampered))

    def test_unknown_prefix_is_rejected(self) -> None:
        user = baker.make(User)
        generate_api_key(user, "Zapier")
        self.assertIsNone(authenticate_api_key(f"{KEY_LABEL}_doesnotexist_somesecret"))

    def test_malformed_key_is_rejected_without_raising(self) -> None:
        self.assertIsNone(authenticate_api_key("not-a-valid-key"))
        self.assertIsNone(authenticate_api_key(""))
        self.assertIsNone(authenticate_api_key("wrong-label_abc_def"))

    def test_revoked_key_is_rejected(self) -> None:
        user = baker.make(User)
        api_key, raw_key = generate_api_key(user, "Zapier")
        revoke_api_key(user, api_key.pk)
        self.assertIsNone(authenticate_api_key(raw_key))

    def test_successful_authentication_updates_last_used_at(self) -> None:
        user = baker.make(User)
        api_key, raw_key = generate_api_key(user, "Zapier")
        self.assertIsNone(api_key.last_used_at)
        authenticate_api_key(raw_key)
        api_key.refresh_from_db()
        self.assertIsNotNone(api_key.last_used_at)

    def test_key_for_a_deactivated_user_is_rejected(self) -> None:
        """revoke_api_key() only fires on explicit user action - an admin disabling
        a compromised account (User.is_active=False) must also cut off its keys."""
        user = baker.make(User)
        _api_key, raw_key = generate_api_key(user, "Zapier")
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertIsNone(authenticate_api_key(raw_key))


class RecordApiKeyUsageTests(TestCase):
    """record_api_key_usage logs activity and keeps each key's trail bounded."""

    def test_records_an_entry_for_the_key(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        record_api_key_usage(api_key, "/dashboard/api/external/v1/whoami/")
        entries = list(ApiKeyUsageLog.objects.for_api_key(api_key))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].endpoint, "/dashboard/api/external/v1/whoami/")

    def test_entries_are_scoped_per_key(self) -> None:
        user = baker.make(User)
        key_one, _ = generate_api_key(user, "One")
        key_two, _ = generate_api_key(user, "Two")
        record_api_key_usage(key_one, "/whoami/")
        self.assertEqual(ApiKeyUsageLog.objects.for_api_key(key_one).count(), 1)
        self.assertEqual(ApiKeyUsageLog.objects.for_api_key(key_two).count(), 0)

    def test_trims_older_entries_beyond_the_limit(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        for i in range(USAGE_LOG_LIMIT + 5):
            record_api_key_usage(api_key, f"/pins/{i}/")
        entries = list(ApiKeyUsageLog.objects.for_api_key(api_key))
        self.assertEqual(len(entries), USAGE_LOG_LIMIT)
        # The most recent entries survive; the oldest were trimmed.
        self.assertEqual(entries[0].endpoint, f"/pins/{USAGE_LOG_LIMIT + 4}/")


class RevokeApiKeyTests(TestCase):
    """revoke_api_key is scoped to the owning user and idempotent."""

    def test_revoking_own_key_succeeds(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        self.assertTrue(revoke_api_key(user, api_key.pk))
        api_key.refresh_from_db()
        self.assertTrue(api_key.is_revoked)

    def test_cannot_revoke_another_users_key(self) -> None:
        owner = baker.make(User)
        attacker = baker.make(User)
        api_key, _raw_key = generate_api_key(owner, "Zapier")
        self.assertFalse(revoke_api_key(attacker, api_key.pk))
        api_key.refresh_from_db()
        self.assertFalse(api_key.is_revoked)

    def test_revoking_an_already_revoked_key_is_a_no_op(self) -> None:
        user = baker.make(User)
        api_key, _raw_key = generate_api_key(user, "Zapier")
        revoke_api_key(user, api_key.pk)
        self.assertFalse(revoke_api_key(user, api_key.pk))

    def test_revoking_unknown_id_returns_false(self) -> None:
        user = baker.make(User)
        self.assertFalse(revoke_api_key(user, 999999))


class GenerateApiKeyPropertyTests(TestCase):
    """Property: whatever name is submitted, the stored name is never empty and never too long."""

    @given(name=st.text(min_size=0, max_size=300))
    @hyp_settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_stored_name_is_always_nonempty_and_bounded(self, name: str) -> None:
        user = baker.make(User)
        ApiKey.objects.all().delete()
        api_key, _raw_key = generate_api_key(user, name)
        self.assertTrue(api_key.name)
        self.assertLessEqual(len(api_key.name), 100)
