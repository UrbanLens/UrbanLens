"""Tests for services.ai.turns - the turn-lifecycle primitives (batch 2c).

No database needed: the lock and turn-record helpers only need a profile
with a ``.pk`` (a real Profile isn't required to exercise them), and
everything else is pure cache/dict logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.turns import (
    MAX_POLL_ATTEMPTS,
    TURN_POLL_INTERVAL_SECONDS,
    acquire_turn_lock,
    claim_turn_proposal,
    new_turn_id,
    read_turn_proposal,
    read_turn_record,
    release_turn_lock,
    store_turn_proposals,
    store_turn_record,
    turn_lock_is_current,
    turn_poll_delay,
)


@dataclass(frozen=True, slots=True)
class _FakeProfile:
    """Just enough of Profile for the lock/cache helpers, which only read .pk."""

    pk: int


class TurnPollDelayTests(SimpleTestCase):
    def test_schedule_grows_then_flattens(self) -> None:
        seen = [turn_poll_delay(i) for i in range(len(TURN_POLL_INTERVAL_SECONDS) + 5)]
        self.assertEqual(seen[: len(TURN_POLL_INTERVAL_SECONDS)], list(TURN_POLL_INTERVAL_SECONDS))
        # Past the schedule's end, every further attempt gets its last value.
        self.assertTrue(all(value == TURN_POLL_INTERVAL_SECONDS[-1] for value in seen[len(TURN_POLL_INTERVAL_SECONDS) :]))

    def test_negative_attempt_is_treated_as_zero(self) -> None:
        self.assertEqual(turn_poll_delay(-5), TURN_POLL_INTERVAL_SECONDS[0])

    def test_max_poll_attempts_is_a_sane_bound(self) -> None:
        # A pacing sanity check, not a hard invariant: catches an accidental
        # order-of-magnitude typo (6 vs 60) without pinning the exact value.
        self.assertGreater(MAX_POLL_ATTEMPTS, len(TURN_POLL_INTERVAL_SECONDS))


class TurnLockTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.profile = _FakeProfile(pk=12345)

    def test_second_acquire_is_refused_while_the_first_holds_it(self) -> None:
        first = acquire_turn_lock(self.profile)
        second = acquire_turn_lock(self.profile)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_release_lets_a_new_acquire_succeed(self) -> None:
        token = acquire_turn_lock(self.profile)
        release_turn_lock(self.profile, token)
        self.assertIsNotNone(acquire_turn_lock(self.profile))

    def test_different_profiles_do_not_contend(self) -> None:
        other = _FakeProfile(pk=67890)
        self.assertIsNotNone(acquire_turn_lock(self.profile))
        self.assertIsNotNone(acquire_turn_lock(other))

    def test_release_with_none_token_is_a_no_op(self) -> None:
        acquire_turn_lock(self.profile)
        release_turn_lock(self.profile, None)
        # Still held - a None token (never acquired) must not release someone else's lock.
        self.assertIsNone(acquire_turn_lock(self.profile))


class TurnLockIsCurrentTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.profile = _FakeProfile(pk=54321)

    def test_true_for_the_current_holder(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        self.assertTrue(turn_lock_is_current(self.profile, token))

    def test_false_once_released(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        release_turn_lock(self.profile, token)
        self.assertFalse(turn_lock_is_current(self.profile, token))

    def test_false_for_a_foreign_or_stale_token(self) -> None:
        acquire_turn_lock(self.profile)
        self.assertFalse(turn_lock_is_current(self.profile, "not-the-real-token"))

    def test_false_once_superseded_by_a_newer_turn(self) -> None:
        stale_token = acquire_turn_lock(self.profile)
        assert stale_token is not None
        release_turn_lock(self.profile, stale_token)
        fresh_token = acquire_turn_lock(self.profile)
        assert fresh_token is not None
        self.assertFalse(turn_lock_is_current(self.profile, stale_token))
        self.assertTrue(turn_lock_is_current(self.profile, fresh_token))


class TurnProposalTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.turn_id = new_turn_id()
        self.proposal = {"n": 0, "tool": "create_trip", "args": {"name": "X"}, "confirm_label": "Create trip"}

    def test_round_trips_with_profile_id(self) -> None:
        store_turn_proposals(self.turn_id, profile_id=42, proposals=[self.proposal])
        self.assertEqual(read_turn_proposal(self.turn_id, 0), {"profile_id": 42, **self.proposal})

    def test_unknown_turn_id_returns_none(self) -> None:
        self.assertIsNone(read_turn_proposal("never-issued", 0))

    def test_out_of_range_index_returns_none(self) -> None:
        store_turn_proposals(self.turn_id, profile_id=42, proposals=[self.proposal])
        self.assertIsNone(read_turn_proposal(self.turn_id, 1))
        self.assertIsNone(read_turn_proposal(self.turn_id, -1))

    def test_claim_succeeds_exactly_once(self) -> None:
        self.assertTrue(claim_turn_proposal(self.turn_id, 0))
        self.assertFalse(claim_turn_proposal(self.turn_id, 0))

    def test_claims_are_independent_per_index(self) -> None:
        self.assertTrue(claim_turn_proposal(self.turn_id, 0))
        self.assertTrue(claim_turn_proposal(self.turn_id, 1))

    def test_claims_are_independent_per_turn(self) -> None:
        other_turn_id = new_turn_id()
        self.assertTrue(claim_turn_proposal(self.turn_id, 0))
        self.assertTrue(claim_turn_proposal(other_turn_id, 0))


class TurnRecordTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()

    def test_round_trips(self) -> None:
        turn_id = new_turn_id()
        store_turn_record(turn_id, profile_id=1, task_id="task-abc", lock_token="tok-1")  # noqa: S106 -- fixture value, not a credential

        record = read_turn_record(turn_id)

        self.assertEqual(record, {"profile_id": 1, "task_id": "task-abc", "lock_token": "tok-1"})

    def test_unknown_turn_id_returns_none(self) -> None:
        self.assertIsNone(read_turn_record("never-issued"))

    def test_turn_ids_are_unique(self) -> None:
        self.assertNotEqual(new_turn_id(), new_turn_id())
