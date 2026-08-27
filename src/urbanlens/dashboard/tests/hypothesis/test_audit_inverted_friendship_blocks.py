"""Tests for the audit_inverted_friendship_blocks management command.

docs/PROBLEMS.md: legacy BLOCKED Friendship rows created before block_profile
started normalizing direction may have from_profile/to_profile backwards, and
there is no stored signal that can prove which ones - so the fix here is a
read-only report of candidates for a human to review, not a data migration.
These tests pin the command's two jobs: reporting the right rows (created
before the cutoff, showing a sign of having been reused rather than freshly
created as a block) and never writing anything.
"""

from __future__ import annotations

import datetime
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship

_COMMAND = "audit_inverted_friendship_blocks"


class AuditInvertedFriendshipBlocksTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = baker.make(User, username="alice").profile
        self.bob = baker.make(User, username="bob").profile
        self.cutoff = "2026-07-30"

    def _run(self, *args) -> str:
        out = StringIO()
        call_command(_COMMAND, *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def _stamp(self, friendship: Friendship, *, created: datetime.datetime, updated: datetime.datetime) -> None:
        Friendship.objects.filter(pk=friendship.pk).update(created=created, updated=updated)

    def test_requires_before(self) -> None:
        with self.assertRaises(CommandError):
            call_command(_COMMAND, stdout=StringIO(), stderr=StringIO())

    def test_rejects_a_malformed_date(self) -> None:
        with self.assertRaises(CommandError):
            call_command(_COMMAND, "--before=not-a-date", stdout=StringIO(), stderr=StringIO())

    def test_no_candidates_reports_nothing_to_review(self) -> None:
        output = self._run(f"--before={self.cutoff}")
        self.assertIn("Nothing to review", output)

    def test_a_row_created_and_blocked_in_the_same_instant_is_reported_as_likely_fine(self) -> None:
        """The always-correct path: Friendship.objects.create(..., status=BLOCKED) for a
        stranger with no prior row. created == updated, no request_message.
        """
        friendship = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.BLOCKED)
        old = timezone.now() - datetime.timedelta(days=60)
        self._stamp(friendship, created=old, updated=old)

        output = self._run(f"--before={self.cutoff}")

        self.assertIn(f"Friendship #{friendship.pk}", output)
        self.assertIn("likely fine", output)
        self.assertIn("0 of 1 row(s) look reused", output)

    def test_a_row_blocked_long_after_it_was_created_is_flagged_for_review(self) -> None:
        """The reuse path the bug lived in: a PENDING/ACCEPTED row later flipped to
        BLOCKED without repointing - updated lands well after created.
        """
        friendship = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.BLOCKED, relationship_type=FriendshipType.FRIEND)
        created = timezone.now() - datetime.timedelta(days=90)
        updated = timezone.now() - datetime.timedelta(days=60)
        self._stamp(friendship, created=created, updated=updated)

        output = self._run(f"--before={self.cutoff}")

        self.assertIn(f"Friendship #{friendship.pk}", output)
        self.assertIn("REVIEW", output)
        self.assertIn("updated", output)
        self.assertIn("1 of 1 row(s) look reused", output)

    def test_a_stored_request_message_flags_a_row_even_with_a_tiny_timestamp_gap(self) -> None:
        """request_message is only ever set at creation (a friend request note) -
        its presence alone proves the row did not start life as a fresh block,
        even if it was blocked moments after being created.
        """
        friendship = Friendship.objects.create(
            from_profile=self.alice,
            to_profile=self.bob,
            status=FriendshipStatus.BLOCKED,
            relationship_type=FriendshipType.FRIEND,
            request_message="hey, let's connect",
        )
        old = timezone.now() - datetime.timedelta(days=60)
        self._stamp(friendship, created=old, updated=old)

        output = self._run(f"--before={self.cutoff}")

        self.assertIn(f"Friendship #{friendship.pk}", output)
        self.assertIn("REVIEW", output)
        self.assertIn("request_message", output)

    def test_a_row_created_on_or_after_the_cutoff_is_excluded(self) -> None:
        friendship = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.BLOCKED)
        recent = timezone.now()
        self._stamp(friendship, created=recent, updated=recent)

        output = self._run("--before=2020-01-01")

        self.assertNotIn(f"Friendship #{friendship.pk}", output)
        self.assertIn("Nothing to review", output)

    def test_a_non_blocked_row_is_never_reported(self) -> None:
        friendship = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.ACCEPTED)
        old = timezone.now() - datetime.timedelta(days=90)
        self._stamp(friendship, created=old, updated=old)

        output = self._run(f"--before={self.cutoff}")

        self.assertNotIn(f"Friendship #{friendship.pk}", output)

    def test_the_command_never_writes(self) -> None:
        friendship = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.BLOCKED, relationship_type=FriendshipType.FRIEND)
        created = timezone.now() - datetime.timedelta(days=90)
        updated = timezone.now() - datetime.timedelta(days=60)
        self._stamp(friendship, created=created, updated=updated)

        self._run(f"--before={self.cutoff}")

        friendship.refresh_from_db()
        self.assertEqual(friendship.from_profile_id, self.alice.pk)
        self.assertEqual(friendship.to_profile_id, self.bob.pk)
        self.assertEqual(friendship.status, FriendshipStatus.BLOCKED)
        self.assertEqual(friendship.created, created)
        self.assertEqual(friendship.updated, updated)
