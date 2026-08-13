"""A backup code must be consumable exactly once, even under concurrent submission.

``verify_totp_code`` claims a TOTP step with a conditional UPDATE and explains why
in a comment: reading the marker and then writing it unconditionally lets two
submissions of one intercepted code - "a phishing proxy replaying it against a
parallel session" - both pass the check before either writes.

``verify_and_consume_backup_code``, in the same module, had the read-then-write
shape that comment warns about: it selected unused codes, matched one in Python,
then wrote ``used_at`` unconditionally. Two racing submissions of the same
intercepted backup code could therefore both succeed.

The interleaving is simulated deterministically rather than with threads: the
hash comparison is patched to consume the row first, which is exactly the state
the losing request would find when it reaches its own write.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import BackupCode
from urbanlens.dashboard.services.auth.two_factor import verify_and_consume_backup_code

_CODE = "abcd-1234"
_NORMALIZED = "ABCD1234"  # _normalize_backup_code upper-cases and strips punctuation


class BackupCodeSingleUseTests(TestCase):
    """One backup code, one successful login."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.backup_code = BackupCode.objects.create(user=self.user, code_hash=make_password(_NORMALIZED))

    def test_a_valid_code_is_accepted_once(self) -> None:
        self.assertTrue(verify_and_consume_backup_code(self.user, _CODE))

        self.backup_code.refresh_from_db()
        self.assertIsNotNone(self.backup_code.used_at)

    def test_the_same_code_is_refused_on_a_second_use(self) -> None:
        verify_and_consume_backup_code(self.user, _CODE)

        self.assertFalse(verify_and_consume_backup_code(self.user, _CODE))

    def test_a_wrong_code_is_refused_and_consumes_nothing(self) -> None:
        self.assertFalse(verify_and_consume_backup_code(self.user, "zzzz-9999"))

        self.backup_code.refresh_from_db()
        self.assertIsNone(self.backup_code.used_at)

    def test_a_code_consumed_concurrently_is_not_accepted_twice(self) -> None:
        """The losing side of the race must not also report success."""
        def consume_then_match(_raw: str, _encoded: str) -> bool:
            # Stand in for a concurrent request that matched and committed its
            # write between this call's read and its own write.
            BackupCode.objects.filter(pk=self.backup_code.pk).update(used_at=timezone.now())
            return True

        with mock.patch("urbanlens.dashboard.services.auth.two_factor.check_password", side_effect=consume_then_match):
            accepted = verify_and_consume_backup_code(self.user, _CODE)

        self.assertFalse(accepted, "a backup code already consumed by a parallel request was accepted a second time")
