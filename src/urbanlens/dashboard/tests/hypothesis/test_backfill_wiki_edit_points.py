"""Tests for `backfill_wiki_edit_points` (services.consensus.points), P90."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import ConsensusRound, ConsensusSession, ConsensusSessionStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.consensus.points import MANUAL_EDIT_POINTS, backfill_wiki_edit_points


class BackfillWikiEditPointsTests(TestCase):
    """`backfill_wiki_edit_points` against a mix of legacy row shapes."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.editor = Profile.objects.get(user=baker.make(User))
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Baseline")

    def _edit(self, **kwargs) -> WikiEdit:
        kwargs.setdefault("wiki", self.wiki)
        kwargs.setdefault("editor", self.editor)
        kwargs.setdefault("changes", {"description": {"from": "Old", "to": "New"}})
        return WikiEdit.objects.create(**kwargs)

    @staticmethod
    def _set_points(edit: WikiEdit, points: int) -> None:
        """Force `consensus_points` to a known value without going through save()/signals."""
        WikiEdit.objects.filter(pk=edit.pk).update(consensus_points=points)

    def test_the_reverting_row_is_marked_is_revert_and_keeps_its_own_award(self) -> None:
        """`reverted_by` is stored on the *target*, pointing at the row that reverted it.

        So it is the reverting row - not the target - that shows up in the reverse `reverts` relation the
        backfill queries, and it is the reverting row that gets `is_revert=True`."""
        target = self._edit()
        reverter = self._edit(changes={"description": {"from": "New", "to": "Old"}})
        target.reverted_by = reverter
        target.save(update_fields=["reverted_by", "updated"])
        self._set_points(reverter, 9)  # simulate a pre-existing award on the reverting row itself

        backfill_wiki_edit_points(WikiEdit)

        reverter.refresh_from_db()
        target.refresh_from_db()
        self.assertTrue(reverter.is_revert, "the row named by another row's reverted_by must be flagged")
        self.assertEqual(reverter.consensus_points, 9, "a revert's own prior award must not be drained")
        self.assertFalse(target.is_revert, "the target being reverted is not itself a revert")

    def test_consensus_scored_rows_are_left_alone(self) -> None:
        """A row with a `consensus_round` was paid in-game already; the flat backfill must skip it."""
        session = ConsensusSession.objects.create(host_profile=self.editor, status=ConsensusSessionStatus.ACTIVE)
        round_ = ConsensusRound.objects.create(
            session=session, sequence_index=0, wiki=self.wiki, field_kind="wiki_name"
        )
        scored = self._edit(consensus_round=round_)
        self._set_points(scored, 20)

        backfill_wiki_edit_points(WikiEdit)

        scored.refresh_from_db()
        self.assertFalse(scored.is_revert)
        self.assertEqual(scored.consensus_points, 20, "an in-game award must not be overwritten by the flat rate")

    def test_unscored_non_consensus_rows_get_the_flat_rate(self) -> None:
        """The actual target of the backfill: a legacy row that predates `consensus_points`."""
        unscored = self._edit()
        self._set_points(unscored, 0)

        backfill_wiki_edit_points(WikiEdit)

        unscored.refresh_from_db()
        self.assertFalse(unscored.is_revert)
        self.assertEqual(unscored.consensus_points, MANUAL_EDIT_POINTS)

    def test_already_scored_non_consensus_rows_are_overwritten_to_the_flat_rate_too(self) -> None:
        """The real behavior, not the defensive one: the second update() has no "already scored" guard.

        Any non-revert row with an editor and no consensus_round matches it, whatever `consensus_points` already
        holds - including a value a live per-diff award (`points_for_changes`) legitimately computed, which need
        not equal the flat `MANUAL_EDIT_POINTS` this stamps on."""
        already_scored = self._edit()
        self._set_points(already_scored, 6)  # e.g. a real weighted multi-field award, != MANUAL_EDIT_POINTS

        backfill_wiki_edit_points(WikiEdit)

        already_scored.refresh_from_db()
        self.assertFalse(already_scored.is_revert)
        self.assertEqual(
            already_scored.consensus_points,
            MANUAL_EDIT_POINTS,
            "the flat backfill has no already-scored guard - it overwrites unconditionally",
        )

    def test_rows_with_no_editor_are_left_alone(self) -> None:
        orphaned = self._edit(editor=None)
        self._set_points(orphaned, 0)

        backfill_wiki_edit_points(WikiEdit)

        orphaned.refresh_from_db()
        self.assertFalse(orphaned.is_revert)
        self.assertEqual(orphaned.consensus_points, 0)

    def test_a_mixed_batch_lands_each_row_independently(self) -> None:
        """One call over every shape at once - the shape the migration itself runs it in."""
        target = self._edit()
        reverter = self._edit(changes={"description": {"from": "New", "to": "Old"}})
        target.reverted_by = reverter
        target.save(update_fields=["reverted_by", "updated"])
        self._set_points(reverter, 9)

        session = ConsensusSession.objects.create(host_profile=self.editor, status=ConsensusSessionStatus.ACTIVE)
        round_ = ConsensusRound.objects.create(
            session=session, sequence_index=0, wiki=self.wiki, field_kind="wiki_name"
        )
        scored = self._edit(consensus_round=round_)
        self._set_points(scored, 20)

        unscored = self._edit()
        self._set_points(unscored, 0)

        backfill_wiki_edit_points(WikiEdit)

        reverter.refresh_from_db()
        target.refresh_from_db()
        scored.refresh_from_db()
        unscored.refresh_from_db()

        self.assertEqual(
            [
                (reverter.is_revert, reverter.consensus_points),
                (target.is_revert, target.consensus_points),
                (scored.is_revert, scored.consensus_points),
                (unscored.is_revert, unscored.consensus_points),
            ],
            [
                (True, 9),
                # target is the row *being* reverted, not the reverting row, so
                # is_revert never applies to it - but it still has an editor, no
                # consensus_round, and is_revert=False, so it matches the flat-rate
                # update like any other ordinary edit.
                (False, MANUAL_EDIT_POINTS),
                (False, 20),
                (False, MANUAL_EDIT_POINTS),
            ],
        )
