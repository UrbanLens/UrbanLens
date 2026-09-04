"""Tests for the TEMPORARY legacy CID coordinate repair.

Delete this file together with
``services.apis.locations.legacy_cid_coordinate_fix`` once every user has had
the chance to re-import their pins.

Parsing (pure, no DB): a name that is wholly a coordinate parses in every
supported format, a name that merely mentions one does not, and a hypothesis
round-trip covers arbitrary in-range pairs.

Matching/moving (DB): a legacy pin is found by CID and by coordinate-shaped
name (verbatim and cross-format) and moved onto its corrected Location, while
post-cutoff pins, other profiles' pins, already-correct pins and collisions with
an existing pin are all left alone.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import (
    LEGACY_COORDINATE_CUTOFF,
    is_legacy_location,
    parse_coordinate_name,
    preview_needs_legacy_repair,
    repair_legacy_pin_coordinates,
)

#: Somewhere the old S2 decode dropped a pin, and where it actually belongs.
WRONG_LATITUDE, WRONG_LONGITUDE = 42.500000, -73.500000
RIGHT_LATITUDE, RIGHT_LONGITUDE = 42.600000, -73.600000


class ParseCoordinateNameTests(TestCase):
    """parse_coordinate_name accepts whole-string coordinates only (pure, no DB)."""

    databases = []

    def test_decimal_pair(self):
        self.assertEqual(parse_coordinate_name("42.1234, -73.5678"), (42.1234, -73.5678))

    def test_decimal_pair_without_space(self):
        self.assertEqual(parse_coordinate_name("42.1234,-73.5678"), (42.1234, -73.5678))

    def test_decimal_pair_space_separated(self):
        self.assertEqual(parse_coordinate_name("42.1234 -73.5678"), (42.1234, -73.5678))

    def test_decimal_pair_parenthesised_and_padded(self):
        self.assertEqual(parse_coordinate_name("  (42.1234, -73.5678)  "), (42.1234, -73.5678))

    def test_two_decimal_places_still_parses(self):
        """The prose scanner needs three decimals to avoid false positives; a whole-string name does not."""
        self.assertEqual(parse_coordinate_name("42.12, -73.57"), (42.12, -73.57))

    def test_hemisphere_suffixed(self):
        latitude, longitude = parse_coordinate_name("42.1234 N, 73.5678 W")
        self.assertAlmostEqual(latitude, 42.1234)
        self.assertAlmostEqual(longitude, -73.5678)

    def test_dms(self):
        latitude, longitude = parse_coordinate_name("42°07'24\"N 73°34'04\"W")
        self.assertAlmostEqual(latitude, 42.123333, places=5)
        self.assertAlmostEqual(longitude, -73.567778, places=5)

    def test_degrees_decimal_minutes(self):
        latitude, longitude = parse_coordinate_name("42°07.404'N 73°34.068'W")
        self.assertAlmostEqual(latitude, 42.1234, places=4)
        self.assertAlmostEqual(longitude, -73.5678, places=4)

    def test_ordinary_place_name_is_not_a_coordinate(self):
        self.assertIsNone(parse_coordinate_name("Abandoned Grain Elevator"))

    def test_name_that_merely_mentions_a_coordinate_is_rejected(self):
        self.assertIsNone(parse_coordinate_name("Bridge near 42.1234, -73.5678"))

    def test_blank_and_none(self):
        self.assertIsNone(parse_coordinate_name(""))
        self.assertIsNone(parse_coordinate_name("   "))
        self.assertIsNone(parse_coordinate_name(None))

    def test_null_island_rejected(self):
        self.assertIsNone(parse_coordinate_name("0.0, 0.0"))

    def test_out_of_range_rejected(self):
        self.assertIsNone(parse_coordinate_name("95.1234, -73.5678"))

    @given(
        latitude=st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False),
    )
    def test_decimal_round_trip(self, latitude: float, longitude: float):
        """Any in-range pair formatted as a name parses back to itself."""
        latitude, longitude = round(latitude, 4), round(longitude, 4)
        if latitude == 0.0 and longitude == 0.0:
            return
        parsed = parse_coordinate_name(f"{latitude:.4f}, {longitude:.4f}")
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed[0], latitude, places=4)
        self.assertAlmostEqual(parsed[1], longitude, places=4)

    @given(name=st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Zs")), min_size=1, max_size=40))
    def test_letters_only_never_parses(self, name: str):
        """A name made only of letters and spaces is never mistaken for a coordinate."""
        self.assertIsNone(parse_coordinate_name(name))


class RepairLegacyPinCoordinatesTests(TestCase):
    """Matching and moving a mis-placed pre-cutoff pin (DB)."""

    def setUp(self):
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.other_profile = self.other_user.profile

    # -- helpers ------------------------------------------------------------

    def _location(self, latitude: float, longitude: float, *, legacy: bool = True) -> Location:
        """Create a Location, backdated before the cutoff unless told otherwise."""
        location = Location.objects.create(latitude=latitude, longitude=longitude)
        stamp = (
            LEGACY_COORDINATE_CUTOFF - timedelta(days=30) if legacy else LEGACY_COORDINATE_CUTOFF + timedelta(days=1)
        )
        Location.objects.filter(pk=location.pk).update(created=stamp)
        return Location.objects.get(pk=location.pk)

    def _pin(self, location: Location, *, name: str = "", profile=None, legacy: bool = True) -> Pin:
        """Create a pin on *location*, backdated before the cutoff unless told otherwise."""
        pin = Pin.objects.create(profile=profile or self.profile, location=location, name=name)
        stamp = (
            LEGACY_COORDINATE_CUTOFF - timedelta(days=30) if legacy else LEGACY_COORDINATE_CUTOFF + timedelta(days=1)
        )
        Pin.objects.filter(pk=pin.pk).update(created=stamp)
        return Pin.objects.get(pk=pin.pk)

    def _set_cid(self, location: Location, cid: int) -> None:
        """Link a CID to *location* without the live place-name lookup.

        Assigning ``location.cid`` goes through ``GooglePlaceService`` with
        ``fetch_if_missing=True``, which calls REData's nearby-places search to
        name the coordinates - a real network call, so under the test harness's
        network block every test in this class died on a ``RuntimeError`` before
        reaching its own assertions. The service's own bulk-path flag skips that
        lookup, which is all these tests ever wanted.
        """
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        GooglePlaceService().set_cid_for_entity(location, cid, fetch_if_missing=False)

    def _repair(self, *, cid=None, name="", latitude=RIGHT_LATITUDE, longitude=RIGHT_LONGITUDE):
        return repair_legacy_pin_coordinates(
            profile=self.profile, cid=cid, name=name, latitude=latitude, longitude=longitude
        )

    # -- is_legacy_location -------------------------------------------------

    def test_is_legacy_location(self):
        self.assertTrue(is_legacy_location(self._location(WRONG_LATITUDE, WRONG_LONGITUDE)))
        self.assertFalse(is_legacy_location(self._location(RIGHT_LATITUDE, RIGHT_LONGITUDE, legacy=False)))

    # -- CID matching -------------------------------------------------------

    def test_moves_legacy_pin_matched_by_cid(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        pin = self._pin(wrong, name="Old Water Tower")

        repaired = self._repair(cid=12345, name="Old Water Tower")

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.pk, pin.pk)
        repaired.refresh_from_db()
        self.assertNotEqual(repaired.location_id, wrong.pk)
        self.assertAlmostEqual(float(repaired.location.latitude), RIGHT_LATITUDE, places=5)
        self.assertAlmostEqual(float(repaired.location.longitude), RIGHT_LONGITUDE, places=5)

    def test_leaves_post_cutoff_pin_alone(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        pin = self._pin(wrong, name="New Water Tower", legacy=False)

        self.assertIsNone(self._repair(cid=12345, name="New Water Tower"))
        pin.refresh_from_db()
        self.assertEqual(pin.location_id, wrong.pk)

    def test_leaves_another_profiles_pin_alone(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        theirs = self._pin(wrong, name="Their Tower", profile=self.other_profile)

        self.assertIsNone(self._repair(cid=12345, name="Their Tower"))
        theirs.refresh_from_db()
        self.assertEqual(theirs.location_id, wrong.pk)

    def test_pin_already_at_corrected_coordinates_is_not_moved(self):
        right = self._location(RIGHT_LATITUDE, RIGHT_LONGITUDE)
        self._set_cid(right, 12345)
        self._pin(right, name="Correct Tower")

        # Nothing to repair - the caller's normal path finds this same pin.
        self.assertIsNone(self._repair(cid=12345, name="Correct Tower"))

    def test_collision_with_existing_pin_is_skipped(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        legacy_pin = self._pin(wrong, name="Old Tower")
        right = self._location(RIGHT_LATITUDE, RIGHT_LONGITUDE)
        self._pin(right, name="Already Here")

        self.assertIsNone(self._repair(cid=12345, name="Old Tower"))
        legacy_pin.refresh_from_db()
        self.assertEqual(legacy_pin.location_id, wrong.pk)

    def test_unknown_cid_matches_nothing(self):
        self._pin(self._location(WRONG_LATITUDE, WRONG_LONGITUDE), name="Old Tower")
        self.assertIsNone(self._repair(cid=999999, name="Old Tower"))

    # -- coordinate-name matching -------------------------------------------

    def test_moves_legacy_pin_matched_by_coordinate_name(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="42.1234, -73.5678")

        repaired = self._repair(name="42.1234, -73.5678")

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.pk, pin.pk)
        repaired.refresh_from_db()
        self.assertNotEqual(repaired.location_id, wrong.pk)

    def test_moves_legacy_pin_matched_by_coordinate_alias(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="Renamed Since Import")
        PinAlias.objects.create(pin=pin, name="42.1234, -73.5678")

        repaired = self._repair(name="42.1234, -73.5678")

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.pk, pin.pk)

    def test_matches_the_same_coordinates_in_a_different_format(self):
        """A stored hemisphere-suffixed name matches an imported signed-decimal one."""
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="42.1234 N, 73.5678 W")
        self.assertEqual(pin.name, "42.1234 N, 73.5678 W", "sanitize_name should leave this form intact")

        repaired = self._repair(name="42.1234, -73.5678")

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.pk, pin.pk)

    def test_matches_a_dms_import_name_against_a_stored_decimal_name(self):
        """The imported name is unsanitized, so DMS parses; the stored side is plain decimal."""
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="42.1233, -73.5678")

        repaired = self._repair(name="42°07'24\"N 73°34'04\"W")

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.pk, pin.pk)

    def test_sanitized_degree_symbols_do_not_produce_a_false_match(self):
        """A stored name whose degree signs sanitize_name stripped no longer parses - and must not match."""
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="42°07'24\"N 73°34'04\"W")
        self.assertNotIn("°", pin.name)

        self.assertIsNone(self._repair(name="51.5074, -0.1278"))
        pin.refresh_from_db()
        self.assertEqual(pin.location_id, wrong.pk)

    def test_different_coordinates_do_not_match(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="42.1234, -73.5678")

        self.assertIsNone(self._repair(name="51.5074, -0.1278"))
        pin.refresh_from_db()
        self.assertEqual(pin.location_id, wrong.pk)

    def test_ordinary_names_never_match_by_name(self):
        """Only coordinate-shaped names are matched textually - real names are far too loose."""
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        pin = self._pin(wrong, name="Abandoned Grain Elevator")

        self.assertIsNone(self._repair(name="Abandoned Grain Elevator"))
        pin.refresh_from_db()
        self.assertEqual(pin.location_id, wrong.pk)

    # -- guards -------------------------------------------------------------

    def test_missing_coordinates_are_a_no_op(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        self._pin(wrong, name="Old Tower")

        self.assertIsNone(self._repair(cid=12345, name="Old Tower", latitude=None, longitude=None))

    def test_non_numeric_coordinates_are_a_no_op(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        self._pin(wrong, name="Old Tower")

        self.assertIsNone(self._repair(cid=12345, name="Old Tower", latitude=float("nan"), longitude=RIGHT_LONGITUDE))

    def test_no_cid_and_no_coordinate_name_matches_nothing(self):
        self._pin(self._location(WRONG_LATITUDE, WRONG_LONGITUDE), name="Old Tower")
        self.assertIsNone(self._repair(cid=None, name="Old Tower"))


class PreviewNeedsLegacyRepairTests(TestCase):
    """preview_needs_legacy_repair - the import preview's "don't dedupe this" flag.

    Regression coverage for the bug where a re-import's preview step deselected
    exactly the pins the legacy repair exists to fix: its "already on your map"
    check compared the preview's own (still S2-guessed) coordinates against the
    user's existing pins, found the legacy pin sitting at that same wrong spot,
    and pre-deselected the record before it ever reached the server-side repair.
    """

    def setUp(self):
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.other_profile = self.other_user.profile

    def _location(self, latitude: float, longitude: float, *, legacy: bool = True) -> Location:
        location = Location.objects.create(latitude=latitude, longitude=longitude)
        stamp = (
            LEGACY_COORDINATE_CUTOFF - timedelta(days=30) if legacy else LEGACY_COORDINATE_CUTOFF + timedelta(days=1)
        )
        Location.objects.filter(pk=location.pk).update(created=stamp)
        return Location.objects.get(pk=location.pk)

    def _pin(self, location: Location, *, name: str = "", profile=None, legacy: bool = True) -> Pin:
        pin = Pin.objects.create(profile=profile or self.profile, location=location, name=name)
        stamp = (
            LEGACY_COORDINATE_CUTOFF - timedelta(days=30) if legacy else LEGACY_COORDINATE_CUTOFF + timedelta(days=1)
        )
        Pin.objects.filter(pk=pin.pk).update(created=stamp)
        return Pin.objects.get(pk=pin.pk)

    def _set_cid(self, location: Location, cid: int) -> None:
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        GooglePlaceService().set_cid_for_entity(location, cid, fetch_if_missing=False)

    def test_true_when_cid_matches_a_legacy_pin(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        self._pin(wrong, name="Old Water Tower")

        self.assertTrue(preview_needs_legacy_repair(self.profile, cid=12345, name="Old Water Tower"))

    def test_false_when_cid_only_matches_a_post_cutoff_pin(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        self._pin(wrong, name="New Water Tower", legacy=False)

        self.assertFalse(preview_needs_legacy_repair(self.profile, cid=12345, name="New Water Tower"))

    def test_false_when_cid_only_matches_another_profiles_pin(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._set_cid(wrong, 12345)
        self._pin(wrong, name="Their Tower", profile=self.other_profile)

        self.assertFalse(preview_needs_legacy_repair(self.profile, cid=12345, name="Their Tower"))

    def test_false_for_unknown_cid_and_ordinary_name(self):
        self._pin(self._location(WRONG_LATITUDE, WRONG_LONGITUDE), name="Old Tower")
        self.assertFalse(preview_needs_legacy_repair(self.profile, cid=999999, name="Old Tower"))

    def test_true_when_name_matches_a_legacy_pin_as_a_coordinate(self):
        wrong = self._location(WRONG_LATITUDE, WRONG_LONGITUDE)
        self._pin(wrong, name="42.1234, -73.5678")

        self.assertTrue(preview_needs_legacy_repair(self.profile, cid=None, name="42.1234, -73.5678"))

    def test_false_for_an_ordinary_name_with_no_cid(self):
        self._pin(self._location(WRONG_LATITUDE, WRONG_LONGITUDE), name="Abandoned Grain Elevator")
        self.assertFalse(preview_needs_legacy_repair(self.profile, cid=None, name="Abandoned Grain Elevator"))
