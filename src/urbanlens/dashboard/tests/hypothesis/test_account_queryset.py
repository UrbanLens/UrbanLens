"""Tests for the account-model querysets: AccountKdf, WebAuthnCredential,
TOTPDevice, and BackupCode.

Part of the ongoing "every model gets its own queryset/manager" cleanup -
these four models were still on the bare default manager despite a literal
copy-pasted `.update_or_create(user=user, defaults={"auth_salt": ...})` call
(AccountKdf, 3x across controllers/account.py and controllers/e2ee.py) and a
`.filter(user=user)` shape repeated across services/webauthn.py and
services/two_factor.py.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import AccountKdf, BackupCode, TOTPDevice, WebAuthnCredential


class AccountKdfForUserTests(TestCase):
    def test_returns_the_users_row(self) -> None:
        user = baker.make(User)
        kdf = AccountKdf.objects.create(user=user, auth_salt="c2FsdA==")
        self.assertEqual(AccountKdf.objects.for_user(user).first(), kdf)

    def test_empty_for_a_user_with_no_row(self) -> None:
        user = baker.make(User)
        self.assertFalse(AccountKdf.objects.for_user(user).exists())

    def test_does_not_match_another_users_row(self) -> None:
        user = baker.make(User)
        other = baker.make(User)
        AccountKdf.objects.create(user=other, auth_salt="c2FsdA==")
        self.assertFalse(AccountKdf.objects.for_user(user).exists())


class AccountKdfSetAuthSaltTests(TestCase):
    def test_creates_a_row_when_none_exists(self) -> None:
        user = baker.make(User)
        kdf, created = AccountKdf.objects.set_auth_salt(user, "c2FsdA==")
        self.assertTrue(created)
        self.assertEqual(kdf.auth_salt, "c2FsdA==")
        self.assertEqual(AccountKdf.objects.for_user(user).count(), 1)

    def test_updates_the_existing_row_instead_of_duplicating(self) -> None:
        user = baker.make(User)
        AccountKdf.objects.create(user=user, auth_salt="b2xk")
        kdf, created = AccountKdf.objects.set_auth_salt(user, "bmV3")
        self.assertFalse(created)
        self.assertEqual(kdf.auth_salt, "bmV3")
        self.assertEqual(AccountKdf.objects.for_user(user).count(), 1)


class WebAuthnCredentialForUserTests(TestCase):
    def test_returns_only_this_users_credentials(self) -> None:
        user = baker.make(User)
        other = baker.make(User)
        mine = baker.make(WebAuthnCredential, user=user)
        baker.make(WebAuthnCredential, user=other)
        self.assertEqual(list(WebAuthnCredential.objects.for_user(user)), [mine])

    def test_empty_for_a_user_with_no_credentials(self) -> None:
        user = baker.make(User)
        self.assertFalse(WebAuthnCredential.objects.for_user(user).exists())


class TOTPDeviceForUserTests(TestCase):
    def test_returns_the_users_device(self) -> None:
        user = baker.make(User)
        device = baker.make(TOTPDevice, user=user)
        self.assertEqual(TOTPDevice.objects.for_user(user).first(), device)

    def test_empty_for_a_user_with_no_device(self) -> None:
        user = baker.make(User)
        self.assertFalse(TOTPDevice.objects.for_user(user).exists())


class BackupCodeForUserAndUnusedForTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)

    def test_for_user_returns_used_and_unused_codes(self) -> None:
        baker.make(BackupCode, user=self.user, used_at=None)
        baker.make(BackupCode, user=self.user, used_at=timezone.now())
        self.assertEqual(BackupCode.objects.for_user(self.user).count(), 2)

    def test_unused_for_excludes_used_codes(self) -> None:
        unused = baker.make(BackupCode, user=self.user, used_at=None)
        baker.make(BackupCode, user=self.user, used_at=timezone.now())
        self.assertEqual(list(BackupCode.objects.unused_for(self.user)), [unused])

    def test_unused_for_does_not_match_another_users_codes(self) -> None:
        other = baker.make(User)
        baker.make(BackupCode, user=other, used_at=None)
        self.assertFalse(BackupCode.objects.unused_for(self.user).exists())
