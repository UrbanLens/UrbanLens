"""Property-based tests for TextChoices utilities and choice enums.

No database access — these are pure logic tests.
"""
from __future__ import annotations

import unittest

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from urbanlens.dashboard.models.abstract.choices import SecurityLevel, TextChoices
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.pin.model import PinStatus, PinType
from urbanlens.dashboard.tests.hypothesis.strategies import (
	friendship_status,
	invalid_security_level,
	pin_status,
	security_level,
)


class SecurityLevelValidTests(unittest.TestCase):
	"""SecurityLevel.valid() and .invalid() are complementary predicates."""

	@given(security_level)
	@settings(max_examples=200)
	def test_valid_returns_true_for_every_canonical_value(self, value: str) -> None:
		"""Every member value must be recognised as valid."""
		self.assertTrue(
			SecurityLevel.valid(value),
			f"Expected SecurityLevel.valid({value!r}) to be True",
		)

	@given(security_level)
	@settings(max_examples=200)
	def test_valid_is_case_insensitive(self, value: str) -> None:
		"""valid() must accept the value in any case variant."""
		self.assertTrue(SecurityLevel.valid(value.upper()))
		self.assertTrue(SecurityLevel.valid(value.lower()))
		self.assertTrue(SecurityLevel.valid(value.title()))

	@given(security_level)
	@settings(max_examples=200)
	def test_invalid_is_complement_of_valid(self, value: str) -> None:
		"""invalid() is the strict complement of valid()."""
		self.assertNotEqual(SecurityLevel.valid(value), SecurityLevel.invalid(value))

	@given(invalid_security_level)
	@settings(max_examples=200)
	def test_invalid_returns_true_for_non_member_strings(self, value: str) -> None:
		"""Strings outside the enum must be flagged as invalid."""
		self.assertTrue(
			SecurityLevel.invalid(value),
			f"Expected SecurityLevel.invalid({value!r}) to be True",
		)

	@given(invalid_security_level)
	@settings(max_examples=200)
	def test_valid_returns_false_for_non_member_strings(self, value: str) -> None:
		"""Strings outside the enum must NOT be accepted by valid()."""
		self.assertFalse(SecurityLevel.valid(value))


class TextChoicesGetNameTests(unittest.TestCase):
	"""TextChoices.get_name() returns the attribute name for any known value."""

	@given(security_level)
	@settings(max_examples=200)
	def test_get_name_returns_non_none_for_valid_value(self, value: str) -> None:
		"""get_name must return a string (not None) for every member value."""
		name = SecurityLevel.get_name(value)
		self.assertIsNotNone(name, f"get_name({value!r}) unexpectedly returned None")
		self.assertIsInstance(name, str)

	@given(security_level)
	@settings(max_examples=200)
	def test_get_name_upper_case_matches_attribute_name(self, value: str) -> None:
		"""The returned name should correspond to a real attribute (upper-case convention)."""
		name = SecurityLevel.get_name(value)
		self.assertIsNotNone(name)
		self.assertTrue(hasattr(SecurityLevel, name), f"SecurityLevel has no attribute {name!r}")

	@given(security_level)
	@settings(max_examples=200)
	def test_get_name_is_case_insensitive_on_input(self, value: str) -> None:
		"""get_name normalises its input to lower-case before comparing."""
		lower_name = SecurityLevel.get_name(value.lower())
		upper_name = SecurityLevel.get_name(value.upper())
		self.assertEqual(lower_name, upper_name)

	@given(invalid_security_level)
	@settings(max_examples=200)
	def test_get_name_returns_none_for_unknown_value(self, value: str) -> None:
		"""Unknown values must return None, not raise."""
		result = SecurityLevel.get_name(value)
		self.assertIsNone(result)

	@given(security_level)
	@settings(max_examples=200)
	def test_get_name_round_trip(self, value: str) -> None:
		"""get_name(value) → name; SecurityLevel[name].value == value."""
		name = SecurityLevel.get_name(value)
		self.assertIsNotNone(name)
		member = SecurityLevel[name]  # type: ignore[misc]
		self.assertEqual(member.value, value)


class PinStatusTests(unittest.TestCase):
	"""PinStatus covers the visited-state vocabulary."""

	@given(pin_status)
	@settings(max_examples=200)
	def test_all_values_are_valid(self, value: str) -> None:
		self.assertTrue(PinStatus.valid(value))

	@given(pin_status)
	@settings(max_examples=200)
	def test_valid_case_insensitive(self, value: str) -> None:
		self.assertTrue(PinStatus.valid(value.upper()))
		self.assertTrue(PinStatus.valid(value.lower()))

	@given(st.sampled_from(list(PinStatus)))
	@settings(max_examples=200)
	def test_every_member_has_a_label(self, member: PinStatus) -> None:
		"""Every member of PinStatus must have a non-empty human-readable label."""
		self.assertTrue(member.label, f"{member!r} has no label")

	def test_known_values_present(self) -> None:
		"""Smoke-test: the four expected statuses are all present."""
		expected = {"not visited", "visited", "wish to visit", "demolished"}
		self.assertEqual(set(PinStatus.values), expected)


class PinTypeTests(unittest.TestCase):
	"""PinType covers the structural vocabulary of a pin."""

	@given(st.sampled_from(list(PinType.values)))
	@settings(max_examples=200)
	def test_all_values_are_valid(self, value: str) -> None:
		self.assertTrue(PinType.valid(value))

	@given(st.sampled_from(list(PinType)))
	@settings(max_examples=200)
	def test_every_member_has_a_label(self, member: PinType) -> None:
		self.assertTrue(member.label)

	def test_location_marker_is_default_value(self) -> None:
		"""The LOCATION_MARKER variant should correspond to the 'location' value."""
		self.assertEqual(PinType.LOCATION_MARKER.value, "location")


class FriendshipStatusPredicateTests(unittest.TestCase):
	"""State-predicate methods on FriendshipStatus."""

	def test_is_friend_only_true_for_accepted(self) -> None:
		"""is_friend must be True for ACCEPTED and False for everything else."""
		for status in FriendshipStatus.values:
			expected = status == FriendshipStatus.ACCEPTED
			self.assertEqual(
				FriendshipStatus.is_friend(status),
				expected,
				f"is_friend({status!r}) should be {expected}",
			)

	def test_rejected_covers_all_non_pending_non_accepted_statuses(self) -> None:
		"""rejected() must return True for the correct set of terminal statuses."""
		should_be_rejected = {
			FriendshipStatus.DECLINED,
			FriendshipStatus.REMOVED,
			FriendshipStatus.BLOCKED,
			FriendshipStatus.MUTED,
			FriendshipStatus.IGNORED,
		}
		for status in FriendshipStatus.values:
			expected = status in should_be_rejected
			self.assertEqual(
				FriendshipStatus.rejected(status),
				expected,
				f"rejected({status!r}) should be {expected}",
			)

	def test_can_request_only_after_declined_or_removed(self) -> None:
		"""can_request() is True only when the previous rejection left the door open."""
		for status in FriendshipStatus.values:
			expected = status in {FriendshipStatus.DECLINED, FriendshipStatus.REMOVED}
			self.assertEqual(
				FriendshipStatus.can_request(status),
				expected,
				f"can_request({status!r}) should be {expected}",
			)

	def test_cannot_request_after_ignored(self) -> None:
		"""IGNORED specifically prevents re-requesting (the block is silent)."""
		self.assertFalse(FriendshipStatus.can_request(FriendshipStatus.IGNORED))

	def test_cannot_request_after_blocked(self) -> None:
		"""BLOCKED must also prevent re-requesting."""
		self.assertFalse(FriendshipStatus.can_request(FriendshipStatus.BLOCKED))

	@given(friendship_status)
	@settings(max_examples=200)
	def test_rejected_and_is_friend_are_mutually_exclusive(self, status: str) -> None:
		"""A friendship cannot be simultaneously accepted and rejected."""
		is_friend = FriendshipStatus.is_friend(status)
		is_rejected = FriendshipStatus.rejected(status)
		self.assertFalse(
			is_friend and is_rejected,
			f"status={status!r} cannot be both friend and rejected",
		)

	@given(friendship_status)
	@settings(max_examples=200)
	def test_can_request_implies_rejected(self, status: str) -> None:
		"""If re-requesting is allowed, the prior relationship must have been rejected."""
		if FriendshipStatus.can_request(status):
			self.assertTrue(
				FriendshipStatus.rejected(status),
				f"can_request is True for {status!r} but rejected is False",
			)

	@given(friendship_status)
	@settings(max_examples=200)
	def test_is_friend_implies_not_can_request(self, status: str) -> None:
		"""An accepted friendship cannot be in the 'can re-request' state."""
		if FriendshipStatus.is_friend(status):
			self.assertFalse(FriendshipStatus.can_request(status))


class FriendshipTypeTests(unittest.TestCase):
	"""Smoke tests for FriendshipType values."""

	def test_all_types_present(self) -> None:
		expected = {"Following", "Friend", "Close Friend"}
		self.assertEqual(set(FriendshipType.values), expected)

	@given(st.sampled_from(list(FriendshipType)))
	@settings(max_examples=50)
	def test_every_member_has_a_label(self, member: FriendshipType) -> None:
		self.assertTrue(member.label)
