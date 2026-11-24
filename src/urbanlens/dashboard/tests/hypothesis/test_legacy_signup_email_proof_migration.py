"""Legacy signup verification cannot safely prove the account's current primary email."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.services.auth.email_normalization import find_verified_user_by_email

_MIGRATION_0032 = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")


class LegacySignupEmailProofMigrationTests(TestCase):
    def _legacy_verified_signup(self, email: str) -> User:
        user = baker.make(User, email=email, is_active=True)
        EmailVerification.objects.create(user=user, verified_at=timezone.now())
        return user

    def test_v08_backfill_does_not_trust_the_current_primary_email(self) -> None:
        user = self._legacy_verified_signup("victim@example.com")

        _MIGRATION_0032._0058_trust_verified_signups(apps, None)

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.verified_primary_email, "")
        self.assertIsNone(find_verified_user_by_email("victim@example.com"))
