"""A building's ``year_built`` is stated as that building's only when REData says it dates the building itself (UrbanLens#321, "a parcel's assessor year").

REData 0.3.6 says what each building's year dates (``year_built_basis``): ``"building"`` for a year a source gave the
building, ``"parcel"`` for the assessor's one year for the parcel's principal improvement, which REData sets on at most
the building under the parcel's lookup point. Older REData, and lists cached before it, say nothing, so their years are
read as the parcel's. No surface here states a parcel's or an unexplained year as when one building was built.
"""

from __future__ import annotations

import importlib
from typing import Any

from django.apps import apps
from django.template.loader import render_to_string
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus
from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource, building_rows
from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource, _render_available
from urbanlens.dashboard.plugins.builtin.redata_building_attributes import _render_building_attributes
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.build_dates import SOURCE_PROPERTY_RECORD, own_build_year, site_build_year
from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster
from urbanlens.dashboard.services.pins.pin_restructure import BuildingNester, building_wiki_name
from urbanlens.dashboard.services.trivia.deterministic import YEAR_BUILT_WITHDRAWN, generate_deterministic_questions
from urbanlens.dashboard.tests.hypothesis.building_fixtures import CAMPUS_LAT, CAMPUS_LNG, rect

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0070_withdraw_year_built_trivia")

_LATITUDE, _LONGITUDE = CAMPUS_LAT, CAMPUS_LNG


def _building(basis: str | None, *, year: Any = 1906, **extra: Any) -> dict[str, Any]:
    """One reconciled building record; ``basis`` None leaves ``year_built_basis`` out, as REData before 0.3.6 did."""
    record = {
        "name": "Catholic Chapel",
        "building_number": "",
        "year_built": year,
        "latitude": _LATITUDE,
        "longitude": _LONGITUDE,
        **extra,
    }
    if basis is not None:
        record["year_built_basis"] = basis
    return record


#: A building whose year is the parcel's, and one whose year nobody explained - the two that date no building.
_NOT_OWN = (("parcel", "parcel"), ("unexplained", None), ("no year", ""))


class OwnBuildYearTests(SimpleTestCase):
    def test_a_year_the_building_was_given_is_its_own(self) -> None:
        self.assertEqual(own_build_year(_building("building")), 1906)
        self.assertEqual(own_build_year(_building("building", year="1871")), 1871)

    def test_a_parcels_year_or_one_nobody_explained_is_not(self) -> None:
        for label, basis in _NOT_OWN:
            with self.subTest(label):
                self.assertIsNone(own_build_year(_building(basis)))

    def test_an_impossible_year_is_not_one_even_when_the_building_was_given_it(self) -> None:
        for year in (None, "", 0, 99, "n/a", True, 3000, float("inf"), float("nan")):
            with self.subTest(year=year):
                self.assertIsNone(own_build_year(_building("building", year=year)))

    @given(
        basis=st.one_of(st.none(), st.text(max_size=12).filter(lambda text: text != "building")),
        year=st.integers(1000, 2000),
    )
    def test_any_basis_but_the_buildings_own_dates_nothing(self, basis: str | None, year: int) -> None:
        self.assertIsNone(own_build_year(_building(basis, year=year)))


class BuildingRowTests(SimpleTestCase):
    """The "Buildings on this Property" rows, which the web panel, the wiki and the external API all render."""

    def _row(self, building: dict[str, Any]) -> dict[str, Any]:
        (row,) = building_rows([building], [])
        return row

    def test_a_row_shows_a_year_the_building_was_given(self) -> None:
        row = self._row(_building("building"))

        self.assertEqual(row["year_built"], 1906)
        self.assertIn(
            "Built 1906", render_to_string("dashboard/partials/pins/_parcel_building_row_summary.html", {"row": row})
        )

    def test_a_row_shows_no_year_that_is_the_parcels_or_unexplained(self) -> None:
        for label, basis in _NOT_OWN:
            with self.subTest(label):
                row = self._row(_building(basis))

                self.assertEqual(row["year_built"], "")
                self.assertNotIn(
                    "1906", render_to_string("dashboard/partials/pins/_parcel_building_row_summary.html", {"row": row})
                )


class BuildingsApiTests(TestCase):
    def test_the_api_row_carries_only_the_buildings_own_year(self) -> None:
        location = baker.make(Location, latitude=_LATITUDE, longitude=_LONGITUDE)
        pin = baker.make(Pin, location=location, parent_pin=None)
        owned = _building("building", name="Chapel", latitude=_LATITUDE + 0.001)
        assessed = _building("parcel", name="Main", year=1874)
        legacy = _building(None, name="Laundry", year=1895, latitude=_LATITUDE - 0.001)
        LocationCache.set(
            location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [owned, assessed, legacy], "provider": "redata"}
        )

        payload = ParcelBuildingsPanelSource().api_payload(pin)

        assert payload is not None
        self.assertEqual(
            {row["name"]: row["year_built"] for row in payload["buildings"]},
            {"Chapel": 1906, "Main": "", "Laundry": ""},
        )


class BuildingAttributesCardTests(SimpleTestCase):
    def test_the_card_states_a_year_the_building_was_given(self) -> None:
        card = _render_building_attributes(_building("building"))

        assert card is not None
        self.assertIn({"label": "Year Built", "value": 1906}, card["meta"])

    def test_the_card_states_no_year_that_is_the_parcels_or_unexplained(self) -> None:
        for label, basis in _NOT_OWN:
            with self.subTest(label):
                card = _render_building_attributes(_building(basis))

                assert card is not None
                self.assertEqual(card["heading_name"], "Catholic Chapel")
                self.assertNotIn("Year Built", [entry["label"] for entry in card["meta"]])

    def test_a_record_with_nothing_but_the_parcels_year_shows_no_card(self) -> None:
        self.assertIsNone(_render_building_attributes(_building("parcel", name="")))


class PropertyRecordsYearTests(SimpleTestCase):
    """The parcel record's year is the assessor's for its principal improvement, and is labelled as such."""

    def test_the_parcel_tab_says_whose_year_it_is(self) -> None:
        labels = {
            row["label"]: row["value"]
            for row in _render_available({"year_built": 1874}, show_owner=False, show_demographics=False)["meta"]
        }

        self.assertEqual(labels.get("Year built (main building)"), 1874)
        self.assertNotIn("Year built", labels)

    def test_the_overview_says_whose_year_it_is(self) -> None:
        summary = PropertyRecordsPanelSource().overview_summary(
            baker.prepare(Pin), {"available": True, "year_built": 1874}
        )

        assert summary is not None
        self.assertIn({"label": "Year built (main building)", "value": "1874"}, summary.fields)


def _questions(location: Location) -> dict[str, tuple[str, str]]:
    return {
        question.dedupe_key: (question.answer, question.status)
        for question in TriviaQuestion.objects.filter(location=location)
    }


class TriviaYearBuiltTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)

    def cache(self, *buildings: dict[str, Any]) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": list(buildings)})

    def test_a_year_the_building_was_given_is_asked(self) -> None:
        self.cache(_building("building"))

        (question,) = generate_deterministic_questions(self.location)

        self.assertEqual((question.prompt, question.answer), ("What year was Catholic Chapel built?", "1906"))

    def test_a_year_that_is_the_parcels_or_unexplained_is_never_asked(self) -> None:
        for label, basis in _NOT_OWN:
            with self.subTest(label):
                self.cache(_building(basis))

                self.assertEqual(generate_deterministic_questions(self.location), [])
                self.assertFalse(TriviaQuestion.objects.approved().filter(location=self.location).exists())

    def _asked_before(self, answer: str = "1874") -> TriviaQuestion:
        """A question generated before UrbanLens read ``year_built_basis``, from whatever year the record carried."""
        return TriviaQuestion.objects.create(
            location=self.location,
            dedupe_key="year_built:Catholic Chapel",
            prompt="What year was Catholic Chapel built?",
            answer=answer,
            source=TriviaQuestionSource.DETERMINISTIC,
        )

    def test_a_question_the_records_no_longer_support_is_withdrawn(self) -> None:
        self._asked_before()
        self.cache(_building("parcel", year=1874))

        generate_deterministic_questions(self.location)

        self.assertEqual(
            _questions(self.location), {"year_built:Catholic Chapel": ("1874", TriviaQuestionStatus.REJECTED)}
        )
        self.assertFalse(TriviaQuestion.objects.approved().filter(location=self.location).exists())

    def test_a_withdrawn_question_returns_with_the_buildings_own_year(self) -> None:
        self._asked_before()
        self.cache(_building(None, year=1874))
        generate_deterministic_questions(self.location)

        self.cache(_building("building", year=1906))
        (question,) = generate_deterministic_questions(self.location)

        self.assertEqual(
            (question.answer, question.status, question.rejection_reason), ("1906", TriviaQuestionStatus.APPROVED, None)
        )
        self.assertEqual(question.answer_normalized, "1906")

    def test_an_answer_the_buildings_own_year_contradicts_is_corrected(self) -> None:
        self._asked_before("1874")
        self.cache(_building("building", year=1906))

        generate_deterministic_questions(self.location)

        self.assertEqual(
            _questions(self.location), {"year_built:Catholic Chapel": ("1906", TriviaQuestionStatus.APPROVED)}
        )

    def test_a_question_whose_building_a_partial_list_leaves_out_is_kept(self) -> None:
        """A list REData answered without one of its sources says nothing about the buildings it does not name."""
        self._asked_before()
        laundry = {**_building("building", year=1895), "name": "Laundry"}
        LocationCache.set(
            self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [laundry], UNANSWERED_SOURCES_KEY: ["cris"]}
        )

        generate_deterministic_questions(self.location)

        self.assertEqual(
            _questions(self.location),
            {
                "year_built:Catholic Chapel": ("1874", TriviaQuestionStatus.APPROVED),
                "year_built:Laundry": ("1895", TriviaQuestionStatus.APPROVED),
            },
        )

    def test_a_partial_list_still_withdraws_a_question_its_own_record_contradicts(self) -> None:
        self._asked_before()
        LocationCache.set(
            self.location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": [_building("parcel", year=1874)], UNANSWERED_SOURCES_KEY: ["overture"]},
        )

        generate_deterministic_questions(self.location)

        self.assertEqual(
            _questions(self.location), {"year_built:Catholic Chapel": ("1874", TriviaQuestionStatus.REJECTED)}
        )

    def test_with_no_building_list_cached_nothing_is_judged(self) -> None:
        self._asked_before()
        withdrawn = TriviaQuestion.objects.create(
            location=self.location,
            dedupe_key="year_built:Laundry",
            prompt="?",
            answer="1895",
            source=TriviaQuestionSource.DETERMINISTIC,
            status=TriviaQuestionStatus.REJECTED,
            rejection_reason=YEAR_BUILT_WITHDRAWN,
        )

        self.assertEqual(generate_deterministic_questions(self.location), [])

        self.assertEqual(
            _questions(self.location),
            {
                "year_built:Catholic Chapel": ("1874", TriviaQuestionStatus.APPROVED),
                withdrawn.dedupe_key: ("1895", TriviaQuestionStatus.REJECTED),
            },
        )

    def test_other_deterministic_questions_are_left_alone(self) -> None:
        number = TriviaQuestion.objects.create(
            location=self.location,
            dedupe_key="building_number:Catholic Chapel",
            prompt="?",
            answer="28",
            source=TriviaQuestionSource.DETERMINISTIC,
        )
        self.cache(_building("parcel", building_number="28"))

        generate_deterministic_questions(self.location)

        number.refresh_from_db()
        self.assertEqual(number.status, TriviaQuestionStatus.APPROVED)


class WithdrawLegacyYearQuestionsMigrationTests(TestCase):
    """0070 withdraws every year-built question generated before UrbanLens read ``year_built_basis``."""

    def test_every_approved_deterministic_year_question_is_withdrawn(self) -> None:
        location = baker.make(Location)
        year = TriviaQuestion.objects.create(
            location=location,
            dedupe_key="year_built:Chapel",
            prompt="?",
            answer="1874",
            source=TriviaQuestionSource.DETERMINISTIC,
        )
        number = TriviaQuestion.objects.create(
            location=location,
            dedupe_key="building_number:Chapel",
            prompt="?",
            answer="28",
            source=TriviaQuestionSource.DETERMINISTIC,
        )
        written = TriviaQuestion.objects.create(
            location=location,
            prompt="What year was the chapel built?",
            answer="1874",
            source=TriviaQuestionSource.USER_SUBMITTED,
        )

        _MIGRATION.withdraw_year_built_questions(apps, None)

        for question in (year, number, written):
            question.refresh_from_db()
        self.assertEqual((year.status, year.rejection_reason), (TriviaQuestionStatus.REJECTED, YEAR_BUILT_WITHDRAWN))
        self.assertEqual(number.status, TriviaQuestionStatus.APPROVED)
        self.assertEqual(written.status, TriviaQuestionStatus.APPROVED)

    def test_the_migration_writes_the_reason_the_generator_reads(self) -> None:
        self.assertEqual(_MIGRATION.WITHDRAWN, YEAR_BUILT_WITHDRAWN)


def _cluster(*members: dict[str, Any]) -> BuildingCluster:
    cluster = BuildingCluster(representative=members[0], latitude=_LATITUDE, longitude=_LONGITUDE)
    for member in members:
        cluster.add(member, None)
    return cluster


class BuildingWikiNameTests(SimpleTestCase):
    """A nameless building's wiki is named for what it is, and dated only by its own year."""

    def _garage(self, basis: str | None) -> BuildingCluster:
        return _cluster(
            _building(basis, name="", year=1925, sources=[{"source": "overpass", "attributes": {"building": "garage"}}])
        )

    def test_a_year_the_building_was_given_dates_its_name(self) -> None:
        self.assertEqual(
            building_wiki_name(self._garage("building"), "Hudson River State Hospital"),
            "Garage (1925) at Hudson River State Hospital",
        )

    def test_a_year_that_is_the_parcels_or_unexplained_does_not(self) -> None:
        for label, basis in _NOT_OWN:
            with self.subTest(label):
                self.assertEqual(
                    building_wiki_name(self._garage(basis), "Hudson River State Hospital"),
                    "Garage at Hudson River State Hospital",
                )


class SiteBuildYearTests(TestCase):
    """The root pin's year: the parcel's is the property's, never a building's."""

    def _root(self, pin_type: str) -> tuple[Pin, BuildingNester]:
        location = baker.make(Location, latitude=_LATITUDE, longitude=_LONGITUDE)
        pin = baker.make(Pin, location=location, parent_pin=None, pin_type=pin_type, pin_type_is_user_provided=True)
        LocationCache.set(location, "property_records", {"available": True, "year_built": 1874})
        main = _building("parcel", year=1874, geometry=rect(0, 0, 40, 40))
        return pin, BuildingNester.build(pin, [main], None)

    def test_a_property_takes_the_parcels_year(self) -> None:
        pin, nester = self._root(PinType.PARCEL)

        built = site_build_year(pin, nester)

        assert built is not None
        self.assertEqual((built.year, built.source), (1874, SOURCE_PROPERTY_RECORD))

    def test_a_building_pinned_on_its_own_does_not(self) -> None:
        pin, nester = self._root(PinType.BUILDING)

        self.assertIsNone(site_build_year(pin, nester))
