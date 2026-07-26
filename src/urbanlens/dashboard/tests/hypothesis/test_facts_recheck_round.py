"""Tests for Consensus's Facts-driven recheck-round selection (services.consensus.selection).

``_pick_recheck_round`` is tested directly against a hand-built wiki pool
(mirrors ``test_consensus_photos.py``'s style of exercising a strategy
function directly rather than through the full eligibility pipeline); the
probability-gated wiring into ``pick_next_round_content`` is tested
separately with the underlying selection mocked out, isolating "does this
get called and its result returned" from "does it pick the right wiki."
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.models.facts.model import Fact, FactStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus import fields, selection


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class PickRecheckRoundTests(TestCase):
    def test_a_tentative_fact_yields_a_recheck_round_for_its_field(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        Fact.objects.create(wiki=wiki, key=ConsensusFieldKind.WIKI_DESCRIPTION, data_type="text", status=FactStatus.TENTATIVE, confidence=0.4)

        result = selection._pick_recheck_round([wiki])

        self.assertIsNotNone(result)
        self.assertEqual(result.wiki, wiki)
        self.assertEqual(result.field_kind, ConsensusFieldKind.WIKI_DESCRIPTION)
        self.assertFalse(result.is_check_round)
        self.assertIsNone(result.known_value)

    def test_a_contested_fact_also_yields_a_recheck_round(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        Fact.objects.create(wiki=wiki, key=ConsensusFieldKind.WIKI_NAME, data_type="text", status=FactStatus.CONTESTED, confidence=0.5)

        result = selection._pick_recheck_round([wiki])

        self.assertIsNotNone(result)
        self.assertEqual(result.field_kind, ConsensusFieldKind.WIKI_NAME)

    def test_no_facts_at_all_returns_none(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        self.assertIsNone(selection._pick_recheck_round([wiki]))

    def test_confirmed_facts_are_not_recheck_candidates(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        Fact.objects.create(wiki=wiki, key=ConsensusFieldKind.WIKI_NAME, data_type="text", status=FactStatus.CONFIRMED, confidence=0.9)
        self.assertIsNone(selection._pick_recheck_round([wiki]))

    def test_wiki_alias_is_not_a_recheck_candidate(self) -> None:
        """WIKI_ALIAS is excluded from Facts entirely - a stray row for it should never surface here."""
        wiki = baker.make(Wiki, location=baker.make(Location))
        Fact.objects.create(wiki=wiki, key=ConsensusFieldKind.WIKI_ALIAS, data_type="text", status=FactStatus.TENTATIVE, confidence=0.4)
        self.assertIsNone(selection._pick_recheck_round([wiki]))

    def test_empty_pool_returns_none(self) -> None:
        self.assertIsNone(selection._pick_recheck_round([]))


class PickNextRoundContentRecheckWiringTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.wiki = baker.make(Wiki, location=baker.make(Location))
        self.sentinel = selection.RoundSelection(wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME, content=fields.RoundContent(), is_check_round=False, known_value=None)
        self.enterContext(mock.patch("urbanlens.dashboard.services.consensus.eligibility.eligible_wikis", return_value=[self.wiki]))
        self.enterContext(mock.patch("urbanlens.dashboard.services.consensus.selection.should_inject_check", return_value=False))

    def test_a_successful_roll_uses_the_recheck_selection(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.consensus.selection.random.random", return_value=0.0),
            mock.patch("urbanlens.dashboard.services.consensus.selection._pick_recheck_round", return_value=self.sentinel),
        ):
            result = selection.pick_next_round_content([self.profile])
        self.assertIs(result, self.sentinel)

    def test_a_failed_roll_never_calls_the_recheck_selection(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.consensus.selection.random.random", return_value=0.999),
            mock.patch("urbanlens.dashboard.services.consensus.selection._pick_recheck_round") as recheck_mock,
        ):
            selection.pick_next_round_content([self.profile])
        recheck_mock.assert_not_called()

    def test_a_successful_roll_with_no_recheck_candidates_falls_through_to_a_normal_round(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.consensus.selection.random.random", return_value=0.0),
            mock.patch("urbanlens.dashboard.services.consensus.selection._pick_recheck_round", return_value=None) as recheck_mock,
            mock.patch("urbanlens.dashboard.services.consensus.selection._pick_normal_round", return_value=self.sentinel) as normal_mock,
        ):
            result = selection.pick_next_round_content([self.profile])
        recheck_mock.assert_called_once()
        normal_mock.assert_called_once()
        self.assertIs(result, self.sentinel)
