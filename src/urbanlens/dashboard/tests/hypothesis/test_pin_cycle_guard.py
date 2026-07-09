"""Property-based tests for Pin.would_create_cycle.

Pin.parent_pin allows pins to be nested to arbitrary depth (a detail pin may
itself have detail pins). The only thing that must never happen is a loop in
that chain (A -> B -> C -> A), since future code walks the chain assuming it
terminates. Key invariants:

1. A None parent never creates a cycle.
2. A pin can never become its own parent.
3. Making a pin the parent of its own ancestor closes a loop and is rejected.
4. This holds at arbitrary chain depth, not just directly adjacent pins.
5. Unrelated pins never register as a cycle.
6. The check terminates even against data that already contains a corrupted cycle.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _make_pin(**kwargs) -> Pin:
    # Pin.profile is a required FK to Profile, which is itself created via a
    # post_save signal on User - letting baker auto-generate the Profile FK
    # directly (rather than going through a User first) creates a second,
    # colliding Profile row for the same auto-generated user.
    kwargs.setdefault("profile", baker.make("auth.User").profile)
    return baker.make(Pin, **kwargs)


class PinCycleGuardBasicTests(TestCase):
    """Straightforward, non-hypothesis cases."""

    def test_none_parent_never_creates_cycle(self) -> None:
        pin = _make_pin()
        self.assertFalse(pin.would_create_cycle(None))

    def test_pin_cannot_be_its_own_parent(self) -> None:
        pin = _make_pin()
        self.assertTrue(pin.would_create_cycle(pin))

    def test_unrelated_pins_are_not_a_cycle(self) -> None:
        a = _make_pin()
        b = _make_pin()
        self.assertFalse(a.would_create_cycle(b))

    def test_making_existing_child_the_new_parent_is_a_cycle(self) -> None:
        """A -> B already exists; A.would_create_cycle(B) must be True (would close A -> B -> A)."""
        a = _make_pin()
        b = _make_pin(parent_pin=a)
        self.assertTrue(a.would_create_cycle(b))

    def test_grandchild_as_new_parent_is_a_cycle(self) -> None:
        """A -> B -> C already exists; A.would_create_cycle(C) must be True."""
        a = _make_pin()
        b = _make_pin(parent_pin=a)
        c = _make_pin(parent_pin=b)
        self.assertTrue(a.would_create_cycle(c))

    def test_unsaved_pin_can_never_be_an_ancestor(self) -> None:
        """A brand-new (unsaved) pin has no pk, so it can't already appear in any chain."""
        existing_parent = _make_pin()
        unsaved = Pin(profile=existing_parent.profile, location=existing_parent.location)
        self.assertFalse(unsaved.would_create_cycle(existing_parent))

    def test_terminates_against_a_preexisting_corrupted_cycle(self) -> None:
        """A pre-existing A<->B loop (bypassing the guard) must not hang an unrelated check."""
        a = _make_pin()
        b = _make_pin(parent_pin=a)
        # Bypass the guard directly to simulate already-corrupted data.
        a.parent_pin = b
        a.save(update_fields=["parent_pin"])

        unrelated = _make_pin()
        self.assertFalse(unrelated.would_create_cycle(a))


class PinCycleGuardDepthTests(TestCase):
    """The guard must hold at arbitrary chain depth, not just directly adjacent pins."""

    @given(depth=st.integers(min_value=1, max_value=8))
    @_db_settings
    def test_closing_the_loop_at_the_far_end_of_a_chain_is_detected(self, depth: int) -> None:
        """Build a depth-long chain root -> ... -> tail, then check tail.would_create_cycle(root)."""
        root = _make_pin()
        chain = [root]
        for _ in range(depth):
            chain.append(_make_pin(parent_pin=chain[-1]))
        tail = chain[-1]
        # Making root's parent the tail would close the loop, unless tail is root itself.
        self.assertTrue(root.would_create_cycle(tail))

    @given(depth=st.integers(min_value=1, max_value=8))
    @_db_settings
    def test_chain_members_are_not_falsely_flagged_against_unrelated_pin(self, depth: int) -> None:
        root = _make_pin()
        chain = [root]
        for _ in range(depth):
            chain.append(_make_pin(parent_pin=chain[-1]))
        unrelated = _make_pin()
        self.assertFalse(unrelated.would_create_cycle(chain[-1]))
