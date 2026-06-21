"""Property-based tests for the Friendship state machine.

Friendship transitions follow strict rules:
  accept  → ACCEPTED
  decline → DECLINED    (re-request allowed)
  ignore  → IGNORED     (re-request blocked)
  remove  → REMOVED     (re-request allowed)
  block   → BLOCKED     (re-request blocked)
  mute    → MUTED       (re-request blocked)

NOTE: Friendship.between() calls QuerySet.get() and raises DoesNotExist when
no friendship exists between two profiles.  The tests here use
Friendship.objects.create() directly for initial setup so they are not affected
by that known limitation.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile

_db_settings = settings(
	max_examples=30,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_DEFAULT_CREATE_KWARGS = dict(
	relationship_type=FriendshipType.FRIEND,
	permissions=Permission.VIEW_PROFILE,
)


def _make_requested(profile_a: Profile, profile_b: Profile) -> Friendship:
	"""Create a REQUESTED friendship directly, bypassing Friendship.request()."""
	return Friendship.objects.create(
		from_profile=profile_a,
		to_profile=profile_b,
		status=FriendshipStatus.REQUESTED,
		**_DEFAULT_CREATE_KWARGS,
	)


class FriendshipTransitionTests(TestCase):
	"""Each instance-method transition must land on the correct status."""

	profile_a: Profile
	profile_b: Profile

	def setUp(self) -> None:
		super().setUp()
		self.profile_a = baker.make(User).profile
		self.profile_b = baker.make(User).profile
		self.friendship = _make_requested(self.profile_a, self.profile_b)

	def test_accept_transitions_to_accepted(self) -> None:
		self.friendship.accept()
		self.friendship.refresh_from_db()
		self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)

	def test_decline_transitions_to_declined(self) -> None:
		self.friendship.decline()
		self.friendship.refresh_from_db()
		self.assertEqual(self.friendship.status, FriendshipStatus.DECLINED)

	def test_ignore_transitions_to_ignored(self) -> None:
		self.friendship.ignore()
		self.friendship.refresh_from_db()
		self.assertEqual(self.friendship.status, FriendshipStatus.IGNORED)

	def test_remove_transitions_to_removed(self) -> None:
		self.friendship.accept()
		self.friendship.remove()
		self.friendship.refresh_from_db()
		self.assertEqual(self.friendship.status, FriendshipStatus.REMOVED)

	def test_accept_makes_is_friend_true(self) -> None:
		self.friendship.accept()
		self.friendship.refresh_from_db()
		self.assertTrue(FriendshipStatus.is_friend(self.friendship.status))

	def test_decline_allows_re_request(self) -> None:
		self.friendship.decline()
		self.friendship.refresh_from_db()
		self.assertTrue(FriendshipStatus.can_request(self.friendship.status))

	def test_remove_allows_re_request(self) -> None:
		self.friendship.accept()
		self.friendship.remove()
		self.friendship.refresh_from_db()
		self.assertTrue(FriendshipStatus.can_request(self.friendship.status))

	def test_ignore_blocks_re_request(self) -> None:
		self.friendship.ignore()
		self.friendship.refresh_from_db()
		self.assertFalse(FriendshipStatus.can_request(self.friendship.status))

	def test_requested_is_not_friend(self) -> None:
		self.assertFalse(FriendshipStatus.is_friend(self.friendship.status))

	def test_initial_status_is_requested(self) -> None:
		self.assertEqual(self.friendship.status, FriendshipStatus.REQUESTED)


class FriendshipBlockMuteTests(TestCase):
	"""block() and mute() classmethods create new friendship rows when none exist."""

	profile_a: Profile
	profile_b: Profile

	def setUp(self) -> None:
		super().setUp()
		self.profile_a = baker.make(User).profile
		self.profile_b = baker.make(User).profile

	def test_block_creates_blocked_friendship(self) -> None:
		f = Friendship.block(self.profile_a, self.profile_b)
		self.assertIsNotNone(f)
		f.refresh_from_db()  # type: ignore[union-attr]
		self.assertEqual(f.status, FriendshipStatus.BLOCKED)  # type: ignore[union-attr]

	def test_mute_creates_muted_friendship(self) -> None:
		f = Friendship.mute(self.profile_a, self.profile_b)
		self.assertIsNotNone(f)
		f.refresh_from_db()  # type: ignore[union-attr]
		self.assertEqual(f.status, FriendshipStatus.MUTED)  # type: ignore[union-attr]

	def test_block_blocks_re_request(self) -> None:
		f = Friendship.block(self.profile_a, self.profile_b)
		self.assertFalse(FriendshipStatus.can_request(f.status))  # type: ignore[union-attr]

	def test_mute_blocks_re_request(self) -> None:
		f = Friendship.mute(self.profile_a, self.profile_b)
		self.assertFalse(FriendshipStatus.can_request(f.status))  # type: ignore[union-attr]

	def test_block_existing_friendship_updates_status(self) -> None:
		existing = _make_requested(self.profile_a, self.profile_b)
		f = Friendship.block(self.profile_a, self.profile_b)
		assert f is not None
		f.refresh_from_db()
		self.assertEqual(f.pk, existing.pk)  # same row, updated
		self.assertEqual(f.status, FriendshipStatus.BLOCKED)


class FriendshipUniqueConstraintTests(TestCase):
	"""Duplicate (from_profile, to_profile) pairs must raise IntegrityError."""

	profile_a: Profile
	profile_b: Profile

	def setUp(self) -> None:
		super().setUp()
		self.profile_a = baker.make(User).profile
		self.profile_b = baker.make(User).profile

	def test_duplicate_friendship_raises_integrity_error(self) -> None:
		_make_requested(self.profile_a, self.profile_b)
		with self.assertRaises(IntegrityError):
			with transaction.atomic():
				_make_requested(self.profile_a, self.profile_b)

	def test_reversed_direction_does_not_conflict(self) -> None:
		"""A→B and B→A are separate friendship rows (unique_together is directional)."""
		_make_requested(self.profile_a, self.profile_b)
		try:
			_make_requested(self.profile_b, self.profile_a)
		except IntegrityError as exc:
			self.fail(f"Reversed pair should not raise IntegrityError: {exc}")


class FriendshipQuerySetTests(TestCase):
	"""Queryset filters: is_friend, not_friend, profile, between."""

	profile_a: Profile
	profile_b: Profile

	def setUp(self) -> None:
		super().setUp()
		self.profile_a = baker.make(User).profile
		self.profile_b = baker.make(User).profile
		self.friendship = _make_requested(self.profile_a, self.profile_b)

	def test_is_friend_filter_only_returns_accepted(self) -> None:
		self.friendship.accept()
		qs = Friendship.objects.all().is_friend()
		for f in qs:
			self.assertEqual(f.status, FriendshipStatus.ACCEPTED)

	def test_not_friend_excludes_accepted(self) -> None:
		self.friendship.accept()
		qs = Friendship.objects.all().not_friend()
		for f in qs:
			self.assertNotEqual(f.status, FriendshipStatus.ACCEPTED)

	def test_between_finds_existing_friendship(self) -> None:
		found = Friendship.objects.all().between(self.profile_a, self.profile_b)
		self.assertIsNotNone(found)
		self.assertEqual(found.pk, self.friendship.pk)

	def test_between_finds_friendship_in_reverse_direction(self) -> None:
		found = Friendship.objects.all().between(self.profile_b, self.profile_a)
		self.assertIsNotNone(found)
		self.assertEqual(found.pk, self.friendship.pk)

	def test_between_returns_none_when_no_friendship_exists(self) -> None:
		"""between() uses .get() but DoesNotExist should be caught when no friendship exists."""
		profile_c: Profile = baker.make(User).profile
		result = Friendship.objects.all().between(self.profile_a, profile_c)
		self.assertIsNone(result)

	def test_profile_filter_includes_both_directions(self) -> None:
		result = set(Friendship.objects.all().profile(self.profile_a).values_list("pk", flat=True))
		self.assertIn(self.friendship.pk, result)
		result_b = set(Friendship.objects.all().profile(self.profile_b).values_list("pk", flat=True))
		self.assertIn(self.friendship.pk, result_b)

	@given(
		status=st.sampled_from([
			FriendshipStatus.DECLINED,
			FriendshipStatus.REMOVED,
			FriendshipStatus.IGNORED,
			FriendshipStatus.BLOCKED,
		])
	)
	@_db_settings
	def test_rejected_statuses_absent_from_is_friend_queryset(self, status: str) -> None:
		self.friendship.status = status
		self.friendship.save()
		qs = Friendship.objects.filter(pk=self.friendship.pk).is_friend()
		self.assertFalse(qs.exists(), f"Status {status!r} must not appear in is_friend()")


class FriendshipPredicateInvariantTests(TestCase):
	"""Class-level predicate invariants (no DB required)."""

	def test_only_accepted_is_friend(self) -> None:
		for status in FriendshipStatus.values:
			expected = status == FriendshipStatus.ACCEPTED
			self.assertEqual(FriendshipStatus.is_friend(status), expected, f"is_friend({status!r}) wrong")

	def test_can_request_subset_of_rejected(self) -> None:
		"""can_request is a strict subset of rejected - anything requestable must also be rejected."""
		for status in FriendshipStatus.values:
			if FriendshipStatus.can_request(status):
				self.assertTrue(
					FriendshipStatus.rejected(status),
					f"can_request({status!r}) is True but rejected({status!r}) is False",
				)

	def test_friend_and_rejected_are_disjoint(self) -> None:
		for status in FriendshipStatus.values:
			self.assertFalse(
				FriendshipStatus.is_friend(status) and FriendshipStatus.rejected(status),
				f"Status {status!r} cannot be both friend and rejected",
			)

	def test_declined_and_removed_allow_re_request(self) -> None:
		for status in (FriendshipStatus.DECLINED, FriendshipStatus.REMOVED):
			self.assertTrue(FriendshipStatus.can_request(status))

	def test_ignored_blocked_muted_block_re_request(self) -> None:
		for status in (FriendshipStatus.IGNORED, FriendshipStatus.BLOCKED, FriendshipStatus.MUTED):
			self.assertFalse(FriendshipStatus.can_request(status))
