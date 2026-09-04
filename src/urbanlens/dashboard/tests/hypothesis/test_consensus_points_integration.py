"""Integration test for the WikiEdit -> Consensus points hook (models.wiki_edit.signals).

This is the behavior most likely to silently regress (a double-award, or a
manual edit never getting credited) - it gets an explicit named test, not
just the pure-logic property tests in test_consensus_points.py.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusProfile,
    ConsensusRound,
    ConsensusSession,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.consensus import points


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class WikiEditPointsHookTests(TestCase):
    def test_a_manual_wiki_edit_awards_baseline_points_exactly_once(self) -> None:
        profile = _make_profile()
        wiki = baker.make(Wiki, location=baker.make(Location))

        WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"name": {"from": "Old", "to": "New"}})

        consensus_profile = ConsensusProfile.objects.get(profile=profile)
        self.assertEqual(consensus_profile.total_points, points.MANUAL_EDIT_POINTS)

    def test_a_second_manual_edit_awards_points_again(self) -> None:
        """Points accumulate across separate manual edits - this isn't a one-time bonus."""
        profile = _make_profile()
        wiki = baker.make(Wiki, location=baker.make(Location))

        WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"name": {"from": "A", "to": "B"}})
        WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"description": {"from": "A", "to": "B"}})

        consensus_profile = ConsensusProfile.objects.get(profile=profile)
        self.assertEqual(consensus_profile.total_points, points.MANUAL_EDIT_POINTS * 2)

    def test_a_consensus_sourced_edit_is_never_double_awarded(self) -> None:
        """A WikiEdit created with consensus_round set must NOT also trigger the manual-edit award.

        Consensus's own resolution code (services.consensus.session) awards
        its (larger) in-game points directly via `points.award_points`
        before creating the WikiEdit - the signal must recognize this edit
        already got its points and skip the baseline award entirely.
        """
        profile = _make_profile()
        wiki = baker.make(Wiki, location=baker.make(Location))
        session = ConsensusSession.objects.create(host_profile=profile, status=ConsensusSessionStatus.ACTIVE)
        round_ = ConsensusRound.objects.create(session=session, sequence_index=0, wiki=wiki, field_kind="wiki_name")

        # Mirrors what services.consensus.session._apply_and_record_edit does:
        # award points directly, then create the WikiEdit with consensus_round set.
        points.award_points(profile.pk, points.SOLO_ANSWER_POINTS, reason="solo_answer")
        WikiEdit.objects.create(
            wiki=wiki, editor=profile, changes={"name": {"from": "Old", "to": "New"}}, consensus_round=round_
        )

        consensus_profile = ConsensusProfile.objects.get(profile=profile)
        self.assertEqual(consensus_profile.total_points, points.SOLO_ANSWER_POINTS)

    def test_an_edit_with_no_editor_awards_nothing(self) -> None:
        """A system/anonymous edit (editor=None) has no profile to credit - must not crash."""
        wiki = baker.make(Wiki, location=baker.make(Location))
        WikiEdit.objects.create(wiki=wiki, editor=None, changes={"name": {"from": "A", "to": "B"}})
        self.assertFalse(ConsensusProfile.objects.exists())
