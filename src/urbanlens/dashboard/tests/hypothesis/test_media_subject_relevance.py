"""Which external media items are about the place they were found for (P196).

Jess's ruling, 2026-10-02: keep an item geolocated inside the place's bounding box, or one naming the place with a
geographic indicator consistent with it; a distinctive name alone is enough when nothing geographic contradicts it,
and a generic name never is. Her examples are the first two test classes, verbatim.
"""

from __future__ import annotations

from dataclasses import replace

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.media.subject_relevance import BoundingBox, MediaSubject, is_distinctive

_LAT, _LNG = 41.7333, -73.9281

HRSH = MediaSubject(
    names=("Hudson River State Hospital", "HRSH", "HRPC"),
    latitude=_LAT,
    longitude=_LNG,
    bbox=BoundingBox.around(_LAT, _LNG, 600),
    city="Poughkeepsie",
    county="Dutchess County",
    state="NY",
    zipcode="12601",
    country="United States",
)
MANSION = replace(HRSH, names=("Historic Mansion",))
STAFF_HOUSE = replace(HRSH, names=("Staff House",), context=("Hudson River State Hospital", "HRSH"))
SEVEN_GABLES = MediaSubject(
    names=("House of the Seven Gables",),
    latitude=42.5222,
    longitude=-70.8850,
    bbox=BoundingBox.around(42.5222, -70.8850, 250),
    city="Salem",
    county="Essex County",
    state="MA",
    zipcode="01970",
    country="United States",
)


def _item(caption: str = "", **fields) -> MediaItem:
    fields.setdefault("url", "https://upload.wikimedia.org/wikipedia/commons/a/ab/Example.jpg")
    return MediaItem(thumb_url="", caption=caption, source="Wikimedia Commons", **fields)


class _TableTestCase(SimpleTestCase):
    def assert_matches(self, subject: MediaSubject, captions: tuple[str, ...], *, expected: bool) -> None:
        for caption in captions:
            with self.subTest(caption=caption):
                judgement = subject.judge(_item(caption))
                self.assertEqual(judgement.relevant, expected, judgement.reason)


class JessExamplesHrshTests(_TableTestCase):
    """Subject: Hudson River State Hospital, Poughkeepsie, Dutchess County, NY 12601; aliases HRSH and HRPC."""

    def test_the_name_with_a_consistent_place_matches(self) -> None:
        self.assert_matches(HRSH, ("HRSH Poughkeepsie", "Hudson River State Hospital NY", "HRPC 12601"), expected=True)

    def test_a_distinctive_name_alone_matches(self) -> None:
        self.assert_matches(HRSH, ("HRSH", "Hudson River State Hospital", "HRPC"), expected=True)

    def test_a_conflicting_city_in_the_same_state_does_not_match(self) -> None:
        self.assert_matches(HRSH, ("HRSH Binghamton",), expected=False)

    def test_part_of_the_name_with_the_place_does_not_match(self) -> None:
        self.assert_matches(HRSH, ("Hudson River Poughkeepsie",), expected=False)

    def test_a_conflicting_state_does_not_match(self) -> None:
        self.assert_matches(HRSH, ("HRPC OH",), expected=False)


class JessExamplesGenericNameTests(_TableTestCase):
    """Subject: a place named "Historic Mansion", in Poughkeepsie."""

    def test_a_generic_name_alone_never_matches(self) -> None:
        self.assert_matches(MANSION, ("Historic Mansion",), expected=False)

    def test_a_generic_name_somewhere_else_does_not_match(self) -> None:
        self.assert_matches(MANSION, ("Historic Mansion, Ohio",), expected=False)

    def test_a_generic_name_with_the_place_matches(self) -> None:
        self.assert_matches(MANSION, ("Historic Mansion Poughkeepsie",), expected=True)


class GeographyTests(_TableTestCase):
    """The assumptions the examples rest on, each tested on its own."""

    def test_a_conflicting_city_outweighs_the_right_state(self) -> None:
        self.assert_matches(
            HRSH, ("HRSH Binghamton NY", "Hudson River State Hospital, Binghamton, New York"), expected=False
        )

    def test_the_right_city_outweighs_a_conflicting_one_mentioned_too(self) -> None:
        self.assert_matches(HRSH, ("HRSH, Poughkeepsie; photographed by a visitor from Binghamton",), expected=True)

    def test_full_state_and_country_names_are_indicators(self) -> None:
        self.assert_matches(
            HRSH,
            ("Hudson River State Hospital, Poughkeepsie, New York", "HRSH, USA", "HRSH, United States"),
            expected=True,
        )
        self.assert_matches(
            HRSH, ("Hudson River State Hospital, Ohio", "Hudson River State Hospital, Germany"), expected=False
        )

    def test_a_city_that_shares_the_states_name_is_a_different_place(self) -> None:
        self.assert_matches(HRSH, ("Hudson River State Hospital, New York City",), expected=False)

    def test_counties(self) -> None:
        self.assert_matches(HRSH, ("HRSH, Dutchess County",), expected=True)
        self.assert_matches(HRSH, ("HRSH, Ulster County",), expected=False)

    def test_the_town_of_the_subjects_city_is_the_subjects_city(self) -> None:
        self.assert_matches(
            HRSH, ("Hudson River Psychiatric Center in the Town of Poughkeepsie, NY, USA",), expected=False
        )
        self.assert_matches(HRSH, ("HRPC in the Town of Poughkeepsie, NY, USA",), expected=True)

    def test_a_city_inside_a_longer_proper_noun_is_not_a_place(self) -> None:
        self.assert_matches(
            HRSH,
            (
                "Hudson River State Hospital, designed after Binghamton State Hospital",
                "George Washington slept near HRSH",
                "Hudson River State Hospital on Washington Street",
            ),
            expected=True,
        )

    def test_postal_codes_that_are_words_are_ignored_in_capitals(self) -> None:
        self.assert_matches(HRSH, ("VIEW OF HRSH IN WINTER", "HRSH OR NOTHING"), expected=True)

    def test_a_different_state_code_in_mixed_case_conflicts(self) -> None:
        self.assert_matches(HRSH, ("Hudson River State Hospital, Salem, OR",), expected=False)

    def test_abbreviated_state_names_are_indicators(self) -> None:
        """Archives and newspapers abbreviate states the old way: "N.Y.", "Mass.", "W. Va."."""
        self.assert_matches(HRSH, ("HRSH, N.Y.", "Hudson River State Hospital (Poughkeepsie, N.Y.)"), expected=True)
        self.assert_matches(
            HRSH,
            ("HRSH, Mass.", "HRSH, Wis.", "HRSH, Calif.", "HRSH, Pa.", "HRSH, W. Va.", "HRSH, W.Va."),
            expected=False,
        )

    def test_an_abbreviation_in_prose_is_a_word(self) -> None:
        self.assert_matches(
            HRSH,
            (
                "HRSH chapel before Mass",
                "Miss Smith at HRSH",
                "HRSH: wash house",
                "Annual report of the HRSH, ill., maps",
                "HRSH chapel; they went to Mass. Then home.",
            ),
            expected=True,
        )

    def test_the_subjects_city_or_county_followed_by_another_state_is_somewhere_else(self) -> None:
        self.assert_matches(
            SEVEN_GABLES,
            (
                "House of the Seven Gables, Salem, Mass.",
                "House of the Seven Gables, Salem, MA",
                "House of the Seven Gables, Salem",
                "House of the Seven Gables, Essex County, Mass.",
            ),
            expected=True,
        )
        self.assert_matches(
            SEVEN_GABLES,
            (
                "House of the Seven Gables, Salem, Ore.",
                "House of the Seven Gables, Salem, Oregon",
                "House of the Seven Gables, Salem, OR",
                "House of the Seven Gables, Essex County, N.J.",
            ),
            expected=False,
        )

    def test_a_city_far_from_the_place_conflicts_and_one_beside_it_does_not(self) -> None:
        self.assert_matches(HRSH, ("HRSH, Albany",), expected=False)
        self.assert_matches(MANSION, ("Historic Mansion, Poughkeepsie, NY",), expected=True)

    def test_a_generic_name_needs_a_local_indicator_not_only_the_state(self) -> None:
        self.assert_matches(MANSION, ("Historic Mansion, NY", "Historic Mansion, New York, USA"), expected=False)
        self.assert_matches(MANSION, ("Historic Mansion, Dutchess County", "Historic Mansion 12601"), expected=True)

    def test_a_generic_name_with_a_conflict_does_not_match_even_beside_the_right_state(self) -> None:
        self.assert_matches(MANSION, ("Historic Mansion, Binghamton, NY",), expected=False)


class NameMatchingTests(_TableTestCase):
    def test_names_match_whole_words_only(self) -> None:
        self.assert_matches(HRSH, ("Hudson River State Hospitality Group", "HRSHX", "XHRPC"), expected=False)

    def test_an_acronym_matches_only_in_capitals(self) -> None:
        self.assert_matches(HRSH, ("hrsh", "Hrsh"), expected=False)

    def test_punctuation_case_and_file_names_do_not_hide_the_name(self) -> None:
        self.assert_matches(
            HRSH,
            (
                "hudson river state hospital",
                "HUDSON RIVER STATE HOSPITAL, POUGHKEEPSIE",
                "Hudson_River_State_Hospital_1871.jpg",
                "H.R.S.H. Poughkeepsie",
                "The Hudson River State Hospital's main building",
            ),
            expected=True,
        )

    def test_no_name_no_match(self) -> None:
        self.assert_matches(HRSH, ("Poughkeepsie, NY", "IMG_0042.jpg", ""), expected=False)

    def test_description_title_and_keywords_are_read_too(self) -> None:
        for item in (
            _item("IMG_0042", description="Hudson River State Hospital in winter"),
            _item("IMG_0042", title="Hudson River State Hospital 1925"),
            _item("IMG_0042", keywords="Hudson River State Hospital|Frederick Clarke Withers"),
        ):
            with self.subTest(item=item):
                self.assertTrue(HRSH.matches(item), HRSH.judge(item).reason)

    def test_tags_written_without_spaces_name_the_place_and_its_state(self) -> None:
        tagged = _item("Hallway", keywords="hudsonriverstatehospital|newyork|abandoned")
        self.assertTrue(MANSION.matches(_item("Hallway", keywords="historicmansion|poughkeepsie")))
        self.assertTrue(HRSH.matches(tagged), HRSH.judge(tagged).reason)
        self.assertFalse(MANSION.matches(_item("Hallway", keywords="historicmansion|newyork")))


class EnclosingSiteTests(_TableTestCase):
    """A building nested under a site: the site's names place it, but are not its own name."""

    def test_the_buildings_name_with_its_sites_name_matches(self) -> None:
        self.assert_matches(
            STAFF_HOUSE, ("Staff House, Hudson River State Hospital", "HRSH staff house"), expected=True
        )

    def test_the_sites_name_alone_is_not_the_building(self) -> None:
        self.assert_matches(STAFF_HOUSE, ("Hudson River State Hospital", "Staff House"), expected=False)


class CoordinateTests(SimpleTestCase):
    def test_a_photo_geolocated_inside_the_box_matches_whatever_its_caption(self) -> None:
        item = _item("IMG_0042.jpg", latitude=_LAT + 0.001, longitude=_LNG - 0.001)
        self.assertTrue(HRSH.matches(item), HRSH.judge(item).reason)

    def test_a_photo_geolocated_far_away_does_not_match_even_naming_the_place(self) -> None:
        binghamton = _item("HRSH Poughkeepsie", latitude=42.0987, longitude=-75.9180)
        self.assertFalse(HRSH.matches(binghamton), HRSH.judge(binghamton).reason)

    def test_a_photo_geolocated_just_outside_the_box_is_judged_by_its_text(self) -> None:
        across_the_river = {"latitude": _LAT, "longitude": _LNG + 0.03}
        self.assertTrue(HRSH.matches(_item("HRSH from across the Hudson", **across_the_river)))
        self.assertFalse(HRSH.matches(_item("IMG_0042.jpg", **across_the_river)))

    def test_zero_zero_is_no_location(self) -> None:
        self.assertTrue(HRSH.matches(_item("HRSH", latitude=0.0, longitude=0.0)))

    def test_the_box_padding_and_distance(self) -> None:
        box = BoundingBox(south=41.0, west=-74.0, north=41.01, east=-73.99)
        self.assertTrue(box.contains(41.005, -73.995))
        self.assertFalse(box.contains(41.02, -73.995))
        self.assertEqual(box.distance_km(41.005, -73.995), 0.0)
        self.assertAlmostEqual(box.distance_km(41.02, -73.995), 1.11, places=1)
        self.assertTrue(box.padded(2000).contains(41.02, -73.995))


class DistinctiveNameTests(SimpleTestCase):
    def test_distinctive(self) -> None:
        for name in (
            "HRSH",
            "HRPC",
            "Hudson River State Hospital",
            "Pennhurst State School",
            "Kings Park Psychiatric Center",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_distinctive(name))

    def test_generic(self) -> None:
        for name in (
            "Historic Mansion",
            "Smith House",
            "Old Main",
            "First Baptist Church",
            "St. Mary's Catholic Church",
            "Union Station",
            "HR",
            "Staff House",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_distinctive(name))
