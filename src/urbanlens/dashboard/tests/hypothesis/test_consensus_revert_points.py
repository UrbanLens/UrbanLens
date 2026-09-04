"""Consensus points around reverts: who gets paid, who gets it taken back, and once.

The defect these pin down: every ``WikiEdit`` with an editor and no
``consensus_round`` earned a flat 3 points, and a revert is itself a
``WikiEdit``. So undoing somebody's work paid the reverter, an edit war paid
both sides on every pass, and a contribution later reverted kept its award -
there was no retraction path anywhere.

The shape of the fix is what most of these tests are really about. Retraction is
a compare-and-swap on a flag stored on the row, not a compensating negative
ledger entry, because several independent paths can reach it for one edit - the
revert itself, an admin toggling ``reverted`` on the change form, deleting an
already-reverted edit - and only the first may move the total. Reverting a
revert has to put the award back, which is why it is a reversible flag rather
than a deletion.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import ConsensusProfile
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.consensus.points import (
    MANUAL_EDIT_POINTS,
    points_for_wiki_edit,
    restore_wiki_edit_award,
    retract_wiki_edit_award,
)
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit, revert_wiki_edit


class ConsensusRevertPointsTests(TestCase):
    """The award and its retraction, across every path that reaches them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.author = Profile.objects.get(user=baker.make(User))
        self.reverter = Profile.objects.get(user=baker.make(User))
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Baseline")

    def _total(self, profile: Profile) -> int:
        row = ConsensusProfile.objects.filter(profile=profile).first()
        return row.total_points if row else 0

    def _edit(self, profile: Profile, **changes) -> WikiEdit:
        return apply_wiki_edit(self.wiki, profile, changes or {"description": "A description of the place."}, strict=False)

    def _revert(self, target: WikiEdit) -> WikiEdit | None:
        self.wiki.refresh_from_db()
        edit, _skipped = revert_wiki_edit(self.location, self.wiki, self.reverter, target)
        return edit

    # -- the filed bug -------------------------------------------------------

    def test_reverting_an_edit_earns_the_reverter_nothing(self) -> None:
        """Undoing somebody's work is not a contribution to pay for."""
        target = self._edit(self.author)

        self._revert(target)

        self.assertEqual(self._total(self.reverter), 0)

    def test_reverting_an_edit_retracts_the_authors_points(self) -> None:
        target = self._edit(self.author)
        earned = self._total(self.author)
        self.assertGreater(earned, 0, "the edit must have paid something for the retraction to mean anything")

        self._revert(target)

        self.assertEqual(self._total(self.author), 0)

    # -- idempotence and reversibility ---------------------------------------

    def test_retraction_is_idempotent(self) -> None:
        target = self._edit(self.author)
        self._revert(target)
        target.refresh_from_db()

        self.assertFalse(retract_wiki_edit_award(target), "the revert already retracted it")
        self.assertEqual(self._total(self.author), 0)

    def test_reverting_a_revert_restores_the_original_authors_points(self) -> None:
        target = self._edit(self.author)
        earned = self._total(self.author)
        revert = self._revert(target)
        self.assertIsNotNone(revert)

        self._revert(revert)

        self.assertEqual(self._total(self.author), earned)

    def test_restoration_is_idempotent(self) -> None:
        target = self._edit(self.author)
        earned = self._total(self.author)
        self._revert(target)
        target.refresh_from_db()
        restore_wiki_edit_award(target)

        target.refresh_from_db()
        self.assertFalse(restore_wiki_edit_award(target))
        self.assertEqual(self._total(self.author), earned)

    def test_admin_toggling_reverted_moves_points_both_ways(self) -> None:
        """The path a service-only design misses: ``reverted`` is editable in the admin."""
        target = self._edit(self.author)
        earned = self._total(self.author)

        target.reverted = True
        target.save(update_fields=["reverted", "updated"])
        self.assertEqual(self._total(self.author), 0)

        target.reverted = False
        target.save(update_fields=["reverted", "updated"])
        self.assertEqual(self._total(self.author), earned)

    def test_an_edit_war_pays_neither_side(self) -> None:
        """The behaviour the whole change is for.

        Alternating reverts used to pay 3 to whoever moved last, every pass,
        forever. Now each pass only moves the one award the original edit
        earned, back and forth, so neither total can grow.
        """
        target = self._edit(self.author)
        earned = self._total(self.author)

        latest: WikiEdit | None = target
        for _ in range(6):
            latest = self._revert(latest)
            if latest is None:
                break

        self.assertEqual(self._total(self.reverter), 0)
        self.assertLessEqual(self._total(self.author), earned)

    # -- the edges that would 500 or double-charge ---------------------------

    def test_reverting_an_edit_whose_editor_was_deleted_does_not_raise(self) -> None:
        """``WikiEdit.editor`` is SET_NULL, so there may be nobody to charge."""
        target = self._edit(self.author)
        WikiEdit.objects.filter(pk=target.pk).update(editor=None)
        target.refresh_from_db()

        revert = self._revert(target)

        self.assertIsNotNone(revert, "the revert itself must still happen")
        self.assertTrue(revert.is_revert)

    def test_retraction_never_creates_a_consensus_profile(self) -> None:
        """A profile with no row is a no-op, not a reason to materialise one at zero."""
        stranger = Profile.objects.get(user=baker.make(User))
        edit = baker.make(WikiEdit, wiki=self.wiki, editor=stranger, changes={"name": {"from": "a", "to": "b"}}, consensus_points=3)
        ConsensusProfile.objects.filter(profile=stranger).delete()

        retract_wiki_edit_award(edit)

        self.assertFalse(ConsensusProfile.objects.filter(profile=stranger).exists())

    def test_a_revert_row_records_no_award_to_take_back(self) -> None:
        target = self._edit(self.author)
        revert = self._revert(target)

        self.assertTrue(revert.is_revert)
        self.assertEqual(revert.consensus_points, 0)
        self.assertEqual(points_for_wiki_edit(revert), 0)

    def test_a_consensus_sourced_edit_is_still_paid_only_once(self) -> None:
        """The pre-existing double-award guard must survive the rewrite."""
        edit = baker.make(
            WikiEdit,
            wiki=self.wiki,
            editor=self.author,
            changes={"name": {"from": "a", "to": "b"}},
            consensus_round=baker.make("dashboard.ConsensusRound"),
        )

        self.assertEqual(points_for_wiki_edit(edit), 0)

    def test_a_plain_edit_still_pays_the_baseline(self) -> None:
        """Guard against the change quietly turning the award off entirely."""
        self._edit(self.author, description="Something substantive about this place.")

        self.assertEqual(self._total(self.author), MANUAL_EDIT_POINTS)
