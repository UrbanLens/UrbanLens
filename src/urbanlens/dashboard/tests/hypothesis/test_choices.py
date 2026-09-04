"""Property-based tests for TextChoices utilities and choice enums.

No database access - these are pure logic tests.
"""

from __future__ import annotations

from hypothesis import assume, given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.abstract.choices import IndoorOutdoor, SecurityLevel, TextChoices
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.pin.model import PIN_TYPE_ICONS, Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.tests.hypothesis.strategies import (
    friendship_status,
    invalid_security_level,
    security_level,
    short_text,
)

# Non-member strings for the choice classes that don't yet have a dedicated
# "invalid" strategy in strategies.py.
_invalid_pin_type = short_text.filter(lambda s: s.lower() not in PinType.values)
_invalid_indoor_outdoor = short_text.filter(lambda s: s.lower() not in IndoorOutdoor.values)


class SecurityLevelValidTests(SimpleTestCase):
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


class TextChoicesGetNameTests(SimpleTestCase):
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
        assert name is not None  # nosec B101
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
        member = SecurityLevel[name]
        self.assertEqual(member.value, value)


class PinTypeTests(SimpleTestCase):
    """PinType covers the structural vocabulary of a pin."""

    @given(st.sampled_from(list(PinType.values)))
    @settings(max_examples=200)
    def test_all_values_are_valid(self, value: str) -> None:
        self.assertTrue(PinType.valid(value))

    @given(_invalid_pin_type)
    @settings(max_examples=200)
    def test_valid_returns_false_for_non_member_strings(self, value: str) -> None:
        """The mirror of test_all_values_are_valid: strings outside the enum must fail."""
        self.assertFalse(PinType.valid(value))

    @given(st.sampled_from(list(PinType)))
    @settings(max_examples=200)
    def test_every_member_has_a_label(self, member: PinType) -> None:
        self.assertTrue(member.label)

    @given(st.sampled_from(list(PinType)))
    @settings(max_examples=200)
    def test_icon_matches_the_declared_mapping(self, member: PinType) -> None:
        """icon must return the glyph declared in PIN_TYPE_ICONS, not the push_pin fallback.

        Guards the shared mapping used by the pin/wiki lists, the type badge, and the
        ``pin_type_icon`` template filter - a member missing its entry would silently
        fall back to "push_pin" instead of failing loudly.
        """
        self.assertEqual(member.icon, PIN_TYPE_ICONS[member.value])

    def test_location_marker_is_default_value(self) -> None:
        """The LOCATION_MARKER variant should correspond to the 'location' value."""
        self.assertEqual(PinType.LOCATION_MARKER.value, "location")


class IndoorOutdoorTests(SimpleTestCase):
    """IndoorOutdoor covers the inside/outside/both vocabulary shared by Pin and Wiki."""

    @given(st.sampled_from(list(IndoorOutdoor.values)))
    @settings(max_examples=200)
    def test_all_values_are_valid(self, value: str) -> None:
        """Every member value must be recognised as valid."""
        self.assertTrue(IndoorOutdoor.valid(value))

    @given(_invalid_indoor_outdoor)
    @settings(max_examples=200)
    def test_valid_returns_false_for_non_member_strings(self, value: str) -> None:
        """The mirror of test_all_values_are_valid: strings outside the enum must fail."""
        self.assertFalse(IndoorOutdoor.valid(value))

    @given(st.sampled_from(list(IndoorOutdoor)))
    @settings(max_examples=200)
    def test_every_member_has_a_label(self, member: IndoorOutdoor) -> None:
        """Every member must have a human-readable display label."""
        self.assertTrue(member.label)

    def test_exactly_three_choices(self) -> None:
        """Only inside/outside/both exist - no separate 'unknown' member; unset is modeled as None."""
        self.assertEqual(set(IndoorOutdoor.values), {"inside", "outside", "both"})

    @given(st.sampled_from([Pin, Wiki]))
    @settings(max_examples=10)
    def test_model_field_accepts_every_choice_and_defaults_unset(self, model: type[Pin | Wiki]) -> None:
        """Pin.indoor_outdoor and Wiki.indoor_outdoor accept all three choices and default to None (unclassified)."""
        field = model._meta.get_field("indoor_outdoor")
        self.assertTrue(field.null, f"{model.__name__}.indoor_outdoor must be null=True (unclassified by default)")
        self.assertTrue(field.blank, f"{model.__name__}.indoor_outdoor must be blank=True")
        self.assertIsNone(
            field.get_default(), f"{model.__name__}.indoor_outdoor must default to None, not a guessed value"
        )
        choice_values = {value for value, _ in field.choices}
        self.assertEqual(choice_values, set(IndoorOutdoor.values))


class FriendshipStatusPredicateTests(SimpleTestCase):
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


class FriendshipTypeTests(SimpleTestCase):
    """Smoke tests for FriendshipType values."""

    def test_all_types_present(self) -> None:
        expected = {"Encountered", "Connected", "Friend", "Close Friend"}
        self.assertEqual(set(FriendshipType.values), expected)

    @given(st.sampled_from(list(FriendshipType)))
    @settings(max_examples=50)
    def test_every_member_has_a_label(self, member: FriendshipType) -> None:
        self.assertTrue(member.label)
