"""Tests for the Consensus field-kind strategy registry (services.consensus.fields).

Pure logic - registry completeness and the text/coordinate agreement rules
each strategy's ``agrees``/``normalize`` implement. No DB required for these
(strategies that need the ORM - find_missing/apply_answer - are exercised by
the DB-backed session tests instead).
"""

from __future__ import annotations

from django.contrib.gis.geos import Point

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.services.consensus.fields import AGREEMENT_DISTANCE_METERS, all_kinds, get_strategy

_HYP = {"max_examples": 100, "deadline": None}
_ascii_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters='\n\r"'), min_size=1, max_size=40
)


class RegistryCompletenessTests(SimpleTestCase):
    def test_every_field_kind_has_a_registered_strategy(self) -> None:
        for kind, _label in ConsensusFieldKind.choices:
            with self.subTest(kind=kind):
                self.assertIsNotNone(get_strategy(kind))

    def test_unknown_kind_has_no_strategy(self) -> None:
        self.assertIsNone(get_strategy("not_a_real_kind"))

    def test_all_kinds_matches_the_choices_list(self) -> None:
        self.assertEqual(set(all_kinds()), {kind for kind, _label in ConsensusFieldKind.choices})


class TextAgreementTests(SimpleTestCase):
    """WIKI_NAME's strategy is representative of every text-kind strategy's shared normalize/agrees."""

    def _strategy(self):
        return get_strategy(ConsensusFieldKind.WIKI_NAME)

    def test_identical_strings_agree(self) -> None:
        strategy = self._strategy()
        self.assertTrue(strategy.agrees("The Old Mill", "The Old Mill"))

    def test_case_and_whitespace_insensitive(self) -> None:
        strategy = self._strategy()
        self.assertTrue(strategy.agrees("The Old Mill", "the   old mill"))

    def test_punctuation_insensitive(self) -> None:
        strategy = self._strategy()
        self.assertTrue(strategy.agrees("St. Mark's", "st marks"))

    def test_different_strings_disagree(self) -> None:
        strategy = self._strategy()
        self.assertFalse(strategy.agrees("The Old Mill", "Something Else"))

    def test_blank_never_agrees_with_anything_including_itself(self) -> None:
        strategy = self._strategy()
        self.assertFalse(strategy.agrees("", ""))
        self.assertFalse(strategy.agrees(None, None))

    @given(value=_ascii_text)
    @settings(**_HYP)
    def test_agreement_is_reflexive_for_nonblank_values(self, value: str) -> None:
        strategy = self._strategy()
        if strategy.normalize(value):
            self.assertTrue(strategy.agrees(value, value))

    @given(a=_ascii_text, b=_ascii_text)
    @settings(**_HYP)
    def test_agreement_is_symmetric(self, a: str, b: str) -> None:
        strategy = self._strategy()
        self.assertEqual(strategy.agrees(a, b), strategy.agrees(b, a))


class ChoiceAgreementTests(SimpleTestCase):
    """WIKI_INDOOR_OUTDOOR/WIKI_PIN_TYPE compare closed-vocabulary values case-insensitively."""

    def test_exact_match_agrees(self) -> None:
        strategy = get_strategy(ConsensusFieldKind.WIKI_INDOOR_OUTDOOR)
        self.assertTrue(strategy.agrees("inside", "inside"))

    def test_case_insensitive(self) -> None:
        strategy = get_strategy(ConsensusFieldKind.WIKI_INDOOR_OUTDOOR)
        self.assertTrue(strategy.agrees("Inside", "inside"))

    def test_different_choices_disagree(self) -> None:
        strategy = get_strategy(ConsensusFieldKind.WIKI_INDOOR_OUTDOOR)
        self.assertFalse(strategy.agrees("inside", "outside"))

    def test_blank_never_agrees(self) -> None:
        strategy = get_strategy(ConsensusFieldKind.WIKI_PIN_TYPE)
        self.assertFalse(strategy.agrees("", ""))
        self.assertFalse(strategy.agrees(None, ""))


class CoordinateAgreementTests(SimpleTestCase):
    def _strategy(self):
        return get_strategy(ConsensusFieldKind.PHOTO_COORDINATES)

    def test_identical_points_agree(self) -> None:
        strategy = self._strategy()
        point = Point(-73.7562, 42.6526, srid=4326)
        self.assertTrue(strategy.agrees(point, point))

    def test_none_never_agrees(self) -> None:
        strategy = self._strategy()
        point = Point(-73.7562, 42.6526, srid=4326)
        self.assertFalse(strategy.agrees(None, point))
        self.assertFalse(strategy.agrees(point, None))
        self.assertFalse(strategy.agrees(None, None))

    def test_far_apart_points_disagree(self) -> None:
        strategy = self._strategy()
        albany = Point(-73.7562, 42.6526, srid=4326)
        nyc = Point(-74.0060, 40.7128, srid=4326)
        self.assertFalse(strategy.agrees(albany, nyc))

    def test_agreement_is_symmetric(self) -> None:
        strategy = self._strategy()
        a = Point(-73.7562, 42.6526, srid=4326)
        b = Point(-73.7563, 42.6527, srid=4326)
        self.assertEqual(strategy.agrees(a, b), strategy.agrees(b, a))

    def test_agreement_distance_threshold_is_positive(self) -> None:
        self.assertGreater(AGREEMENT_DISTANCE_METERS, 0)
