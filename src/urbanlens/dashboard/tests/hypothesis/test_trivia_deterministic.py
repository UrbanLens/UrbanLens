"""Tests for services.trivia.deterministic - question generation from cached parcel-buildings data.

Mocks LocationCache directly rather than the REData gateway - these
generators must never trigger a live fetch, only read what's already cached
(see services.locations.site_scope.parcel_buildings's docstring).
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource
from urbanlens.dashboard.services.trivia.deterministic import (
    BUILDING_COUNT_QUESTION_THRESHOLD,
    generate_deterministic_questions,
)


def _cache_buildings(location: Location, buildings: list[dict]) -> None:
    LocationCache.set(location, "parcel_buildings", {"buildings": buildings})


class YearBuiltQuestionTests(TestCase):
    def test_generates_a_question_for_a_named_building_with_a_year_built(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "The Armory", "building_number": "", "year_built": 1937}])

        questions = generate_deterministic_questions(location)

        self.assertEqual(len(questions), 1)
        question = questions[0]
        self.assertEqual(question.source, TriviaQuestionSource.DETERMINISTIC)
        self.assertEqual(question.answer, "1937")
        self.assertIn("The Armory", question.prompt)

    def test_skips_a_building_with_no_year_built(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "The Armory", "building_number": "", "year_built": None}])
        self.assertEqual(generate_deterministic_questions(location), [])

    def test_skips_a_building_with_no_meaningful_name(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "", "building_number": "", "year_built": 1937}])
        self.assertEqual(generate_deterministic_questions(location), [])

    def test_is_idempotent(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "The Armory", "building_number": "", "year_built": 1937}])

        generate_deterministic_questions(location)
        generate_deterministic_questions(location)

        self.assertEqual(TriviaQuestion.objects.for_location(location).count(), 1)


class BuildingNumberQuestionTests(TestCase):
    def test_generates_a_question_for_a_named_building_with_a_building_number(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "Barracks Hall", "building_number": "12", "year_built": None}])

        questions = generate_deterministic_questions(location)

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].answer, "12")

    def test_skips_a_building_with_no_building_number(self) -> None:
        location = baker.make(Location)
        _cache_buildings(location, [{"name": "Barracks Hall", "building_number": "", "year_built": None}])
        self.assertEqual(generate_deterministic_questions(location), [])


class BuildingCountQuestionTests(TestCase):
    def test_no_question_below_the_threshold(self) -> None:
        location = baker.make(Location)
        buildings = [
            {"name": "", "building_number": "", "year_built": None}
            for _ in range(BUILDING_COUNT_QUESTION_THRESHOLD - 1)
        ]
        _cache_buildings(location, buildings)
        self.assertEqual(generate_deterministic_questions(location), [])

    def test_question_generated_at_the_threshold(self) -> None:
        location = baker.make(Location)
        buildings = [
            {"name": "", "building_number": "", "year_built": None} for _ in range(BUILDING_COUNT_QUESTION_THRESHOLD)
        ]
        _cache_buildings(location, buildings)

        questions = generate_deterministic_questions(location)

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].answer, str(BUILDING_COUNT_QUESTION_THRESHOLD))


class NoCachedDataTests(TestCase):
    def test_no_cache_row_yields_no_questions(self) -> None:
        location = baker.make(Location)
        self.assertEqual(generate_deterministic_questions(location), [])
