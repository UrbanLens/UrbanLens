"""End-to-end tests for Consensus round resolution (services.consensus.session).

Builds a wiki that's missing exactly one piece of data (a description) so
round selection is deterministic - see ``_make_wiki_missing_description``.
Competitive sessions are constructed directly (bypassing the friend-only
invite flow, which is exercised separately in the controller tests) so
these can focus purely on answer/vote resolution.
"""

from __future__ import annotations

from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import IndoorOutdoor
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.consensus.model import (
    ConsensusFieldKind,
    ConsensusProfile,
    ConsensusRoundResolution,
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
    ConsensusSessionStatus,
    ConsensusTentativeAnswer,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.consensus import points, session as consensus_session


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_wiki_missing_description(visited_by: Profile) -> Wiki:
    """A wiki whose only missing/unconfirmed field is its description - deterministic round selection."""
    location = baker.make(Location)
    wiki = baker.make(
        Wiki,
        location=location,
        name="Old Mill Sanatorium",
        description=None,
        indoor_outdoor=IndoorOutdoor.INSIDE,
        pin_type=PinType.BUILDING,
        pin_type_is_user_provided=True,
    )
    baker.make(WikiAlias, wiki=wiki, name="The Mill")
    baker.make(WikiAlias, wiki=wiki, name="Old Mill")
    baker.make(Pin, profile=visited_by, location=location, last_visited=timezone.now())
    return wiki


def _start_competitive_session_directly(host: Profile, invitees: list[Profile]) -> ConsensusSession:
    """Create an ACTIVE, fully-joined competitive session - bypasses the friend-only invite flow."""
    session = ConsensusSession.objects.create(host_profile=host, status=ConsensusSessionStatus.ACTIVE)
    for profile in [host, *invitees]:
        ConsensusSessionParticipant.objects.create(
            session=session, profile=profile, status=ConsensusSessionParticipantStatus.JOINED
        )
    return session


class SoloRoundFlowTests(TestCase):
    def setUp(self) -> None:
        # These tests are about answer/vote *resolution*, not trust-check
        # injection (covered separately in test_consensus_trust.py) - the
        # test wiki deliberately confirms every field except description so
        # round selection is deterministic, but that same "confirmed" state
        # also makes it a valid *check*-round candidate, which would
        # otherwise flakily hijack the round at random (see
        # services.consensus.trust.CHECK_PROBABILITY_MIN/MAX).
        self.enterContext(
            mock.patch("urbanlens.dashboard.services.consensus.selection.should_inject_check", return_value=False)
        )

    def test_solo_answer_applies_immediately_and_awards_points(self) -> None:
        profile = _make_profile()
        wiki = _make_wiki_missing_description(profile)
        session = consensus_session.start_solo_session(profile)

        round_ = consensus_session.get_or_create_round(session)
        self.assertIsNotNone(round_)
        self.assertEqual(round_.field_kind, ConsensusFieldKind.WIKI_DESCRIPTION)
        self.assertEqual(round_.wiki_id, wiki.pk)

        consensus_session.submit_answer(round_, profile, "A description of the mill.")
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.AGREED)
        self.assertEqual(wiki.description, "A description of the mill.")

        edit = WikiEdit.objects.filter(wiki=wiki).first()
        self.assertIsNotNone(edit)
        self.assertEqual(edit.consensus_round_id, round_.pk)
        self.assertEqual(edit.editor_id, profile.pk)

        consensus_profile = ConsensusProfile.objects.get(profile=profile)
        self.assertEqual(consensus_profile.total_points, points.SOLO_ANSWER_POINTS)

    def test_solo_skip_never_touches_the_wiki_or_awards_points(self) -> None:
        profile = _make_profile()
        wiki = _make_wiki_missing_description(profile)
        session = consensus_session.start_solo_session(profile)
        round_ = consensus_session.get_or_create_round(session)

        consensus_session.skip_round(round_, profile)
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.SKIPPED)
        self.assertIsNone(wiki.description)
        self.assertFalse(WikiEdit.objects.filter(wiki=wiki).exists())
        self.assertEqual(ConsensusProfile.objects.get_or_create_for(profile).total_points, 0)

    def test_answering_an_already_settled_round_is_rejected(self) -> None:
        profile = _make_profile()
        _make_wiki_missing_description(profile)
        session = consensus_session.start_solo_session(profile)
        round_ = consensus_session.get_or_create_round(session)
        consensus_session.submit_answer(round_, profile, "First answer.")

        with self.assertRaises(consensus_session.ConsensusError):
            consensus_session.submit_answer(round_, profile, "Second answer.")


class CompetitiveRoundFlowTests(TestCase):
    def setUp(self) -> None:
        # See SoloRoundFlowTests.setUp - same determinism rationale.
        self.enterContext(
            mock.patch("urbanlens.dashboard.services.consensus.selection.should_inject_check", return_value=False)
        )

    def test_full_agreement_applies_once_and_pays_everyone_equally(self) -> None:
        alice = _make_profile()
        bob = _make_profile()
        wiki = _make_wiki_missing_description(alice)
        baker.make(Pin, profile=bob, location=wiki.location, last_visited=timezone.now())

        session = _start_competitive_session_directly(alice, [bob])
        round_ = consensus_session.get_or_create_round(session)

        consensus_session.submit_answer(round_, alice, "Old Mill")
        consensus_session.submit_answer(round_, bob, "old mill")  # same after case-normalization
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.AGREED)
        self.assertIn(wiki.description, ("Old Mill", "old mill"))
        self.assertEqual(WikiEdit.objects.filter(wiki=wiki).count(), 1)
        for profile in (alice, bob):
            self.assertEqual(
                ConsensusProfile.objects.get(profile=profile).total_points, points.COMPETITIVE_AGREE_POINTS
            )

    def test_disagreement_then_vote_consensus_applies_winner_and_pays_winners_more(self) -> None:
        alice = _make_profile()
        bob = _make_profile()
        carol = _make_profile()
        wiki = _make_wiki_missing_description(alice)
        for profile in (bob, carol):
            baker.make(Pin, profile=profile, location=wiki.location, last_visited=timezone.now())

        session = _start_competitive_session_directly(alice, [bob, carol])
        round_ = consensus_session.get_or_create_round(session)

        consensus_session.submit_answer(round_, alice, "Description A")
        consensus_session.submit_answer(round_, bob, "Description A")
        consensus_session.submit_answer(round_, carol, "Description B")
        round_.refresh_from_db()
        self.assertEqual(round_.resolution, ConsensusRoundResolution.VOTE_OPEN)

        answers = {answer.profile_id: answer for answer in round_.answers.all()}
        winning_answer = answers[alice.pk]
        consensus_session.submit_vote(round_, alice, winning_answer)
        consensus_session.submit_vote(round_, bob, winning_answer)
        consensus_session.submit_vote(round_, carol, winning_answer)
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.VOTE_RESOLVED)
        self.assertEqual(wiki.description, "Description A")
        self.assertEqual(ConsensusProfile.objects.get(profile=alice).total_points, points.VOTE_WINNER_POINTS)
        self.assertEqual(ConsensusProfile.objects.get(profile=bob).total_points, points.VOTE_WINNER_POINTS)
        self.assertEqual(ConsensusProfile.objects.get(profile=carol).total_points, points.VOTE_PARTICIPANT_POINTS)

    def test_disagreement_with_no_vote_majority_saves_tentative_and_leaves_wiki_untouched(self) -> None:
        alice = _make_profile()
        bob = _make_profile()
        wiki = _make_wiki_missing_description(alice)
        baker.make(Pin, profile=bob, location=wiki.location, last_visited=timezone.now())

        session = _start_competitive_session_directly(alice, [bob])
        round_ = consensus_session.get_or_create_round(session)
        consensus_session.submit_answer(round_, alice, "Description A")
        consensus_session.submit_answer(round_, bob, "Description B")
        round_.refresh_from_db()
        self.assertEqual(round_.resolution, ConsensusRoundResolution.VOTE_OPEN)

        answers = {answer.profile_id: answer for answer in round_.answers.all()}
        # A perfect 1-vs-1 split: each votes their own answer, no share exceeds the 0.5 threshold.
        consensus_session.submit_vote(round_, alice, answers[alice.pk])
        consensus_session.submit_vote(round_, bob, answers[bob.pk])
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.TENTATIVE)
        self.assertIsNone(wiki.description)
        self.assertFalse(WikiEdit.objects.filter(wiki=wiki).exists())
        self.assertEqual(ConsensusTentativeAnswer.objects.filter(wiki=wiki).count(), 2)
        for profile in (alice, bob):
            self.assertEqual(ConsensusProfile.objects.get(profile=profile).total_points, points.TENTATIVE_POINTS)

    def test_everyone_skipping_settles_as_skipped_with_no_edit_or_points(self) -> None:
        alice = _make_profile()
        bob = _make_profile()
        wiki = _make_wiki_missing_description(alice)
        baker.make(Pin, profile=bob, location=wiki.location, last_visited=timezone.now())

        session = _start_competitive_session_directly(alice, [bob])
        round_ = consensus_session.get_or_create_round(session)
        consensus_session.skip_round(round_, alice)
        consensus_session.skip_round(round_, bob)
        round_.refresh_from_db()
        wiki.refresh_from_db()

        self.assertEqual(round_.resolution, ConsensusRoundResolution.SKIPPED)
        self.assertIsNone(wiki.description)
        self.assertFalse(WikiEdit.objects.filter(wiki=wiki).exists())
