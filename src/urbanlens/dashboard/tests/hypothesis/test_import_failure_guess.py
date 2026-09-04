"""Guessing a location for an unplaceable import, from the pin's name.

An import whose CID never resolves leaves a `PinImportFailure` carrying little
more than a name, and the user places each one by hand - hundreds per import.
Many of those names are geocodable: exported names are frequently bare addresses
("123 Main St", usually with no city), and a name that is not an address is still
often a place OpenStreetMap knows.

Every case here is a *suggestion*; nothing is placed automatically. The tests
therefore care as much about what is refused as what is offered - a wrong
suggestion costs the user more than no suggestion, because they have to notice it
is wrong.

Nominatim is stubbed throughout: this is about the decision logic, and the
network is unavailable in tests by design.
"""

from __future__ import annotations

import math
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.import_failure_guess import guess_for_failure

_GATEWAY = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway"
_S2 = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.extract_coordinates_from_url"


def _osm(lat: str, lon: str, *, name: str = "Somewhere", cls: str = "building", importance: float = 0.6) -> dict:
    return {"lat": lat, "lon": lon, "display_name": name, "class": cls, "importance": importance}


class ImportFailureGuessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _failure(self, name: str, *, maps_url: str = "") -> PinImportFailure:
        return PinImportFailure.objects.create(
            profile=self.profile,
            cid=12345,
            name=name,
            maps_url=maps_url,
            reason=PinImportFailureReason.LOOKUP_STALLED,
        )

    def test_a_bare_street_address_is_geocoded(self) -> None:
        """The common export shape: a house number and street, no city."""
        failure = self._failure("123 Main St")

        with mock.patch(
            f"{_GATEWAY}.search", return_value=[_osm("41.35", "-71.45", name="123 Main St, Newport")]
        ) as search:
            guess = guess_for_failure(failure)

        self.assertIsNotNone(guess)
        self.assertEqual(guess.source, "address")
        self.assertAlmostEqual(guess.latitude, 41.35)
        self.assertIn("123 Main St", search.call_args_list[0].args[0])

    def test_a_place_name_falls_back_to_an_osm_search(self) -> None:
        failure = self._failure("Fort Wetherill")

        with mock.patch(
            f"{_GATEWAY}.search",
            return_value=[_osm("41.47", "-71.35", name="Fort Wetherill", cls="historic", importance=0.5)],
        ):
            guess = guess_for_failure(failure)

        self.assertIsNotNone(guess)
        self.assertEqual(guess.source, "name")

    def test_a_faint_name_match_is_refused(self) -> None:
        """A weak match on a generic name is worse than no suggestion."""
        failure = self._failure("The Mill")

        with mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.47", "-71.35", cls="building", importance=0.05)]):
            self.assertIsNone(guess_for_failure(failure))

    def test_a_road_or_postcode_match_is_refused(self) -> None:
        """Not the sort of thing anyone pinned."""
        failure = self._failure("Fort Wetherill")

        with mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.47", "-71.35", cls="highway", importance=0.9)]):
            self.assertIsNone(guess_for_failure(failure))

    def test_nothing_found_yields_no_guess(self) -> None:
        failure = self._failure("Fort Wetherill")

        with mock.patch(f"{_GATEWAY}.search", return_value=[]):
            self.assertIsNone(guess_for_failure(failure))

    def test_a_too_short_name_is_not_looked_up_at_all(self) -> None:
        """Avoids a pointless request per failure on junk names."""
        failure = self._failure("X")

        with mock.patch(f"{_GATEWAY}.search") as search:
            self.assertIsNone(guess_for_failure(failure))

        search.assert_not_called()

    def test_an_area_hint_discards_a_far_away_match(self) -> None:
        """The S2 cell is used to *narrow*, never to place - see the module docstring."""
        failure = self._failure("123 Main St")

        with mock.patch(f"{_GATEWAY}.search", return_value=[_osm("51.50", "-0.12", name="123 Main St, London")]):
            guess = guess_for_failure(failure, near=(41.35, -71.45))

        self.assertIsNone(guess)

    def test_an_area_hint_keeps_a_nearby_match(self) -> None:
        failure = self._failure("123 Main St")

        with mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.36", "-71.46", name="123 Main St, Newport")]):
            guess = guess_for_failure(failure, near=(41.35, -71.45))

        self.assertIsNotNone(guess)

    def test_a_geocoder_failure_is_swallowed(self) -> None:
        """A guess is a convenience; it must never break the failures page."""
        failure = self._failure("123 Main St")

        with mock.patch(f"{_GATEWAY}.search", side_effect=RuntimeError("nominatim down")):
            self.assertIsNone(guess_for_failure(failure))

    def test_an_unusable_result_does_not_raise(self) -> None:
        failure = self._failure("Fort Wetherill")

        with mock.patch(
            f"{_GATEWAY}.search", return_value=[{"display_name": "no coordinates", "class": "place", "importance": 0.9}]
        ):
            self.assertIsNone(guess_for_failure(failure))


class ImportFailureGuessCorroborationTests(TestCase):
    """The S2 cell raises confidence when it agrees, and never rejects when it does not.

    Decoding the cell is wrong about one time in three, which was fatal when the
    importer used it to *place* pins (see the module docstring) but is useful for
    proposing one: two independent signals agreeing is much stronger evidence
    than either alone, and a disagreeing cell must not veto a good match at that
    error rate.
    """

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _failure(self, name: str) -> PinImportFailure:
        return PinImportFailure.objects.create(
            profile=self.profile,
            cid=12345,
            name=name,
            maps_url="https://maps.google.com/?cid=12345&data=!1s0x89c25a1b:0x3039",
            reason=PinImportFailureReason.LOOKUP_STALLED,
        )

    def test_an_agreeing_cell_raises_confidence(self) -> None:
        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.35", "-71.45")]),
            mock.patch(_S2, return_value=(41.36, -71.46)),
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "address+area")
        self.assertGreater(guess.confidence, 0.9)

    def test_a_disagreeing_cell_does_not_reject_the_match(self) -> None:
        """It is wrong a third of the time; vetoing would discard good guesses."""
        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.35", "-71.45")]),
            mock.patch(_S2, return_value=(51.50, -0.12)),
        ):
            guess = guess_for_failure(failure)

        self.assertIsNotNone(guess)
        self.assertEqual(guess.source, "address")

    def test_a_corroborated_name_match_beats_a_more_important_one(self) -> None:
        """Two signals on the same place beats one pointing harder elsewhere."""
        failure = self._failure("The Mill")
        far_but_important = _osm("51.50", "-0.12", name="The Mill, London", importance=0.9)
        near_and_agreeing = _osm("41.36", "-71.46", name="The Mill, Newport", importance=0.4)

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[far_but_important, near_and_agreeing]),
            mock.patch(_S2, return_value=(41.35, -71.45)),
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "name+area")
        self.assertIn("Newport", guess.display_name)

    def test_the_cell_alone_is_offered_as_a_rough_area(self) -> None:
        """Better than nothing when the name finds nothing at all."""
        failure = self._failure("Unfindable Place")

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[]),
            mock.patch(_S2, return_value=(41.35, -71.45)),
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "area")
        self.assertLess(guess.confidence, 0.5, "an area-only guess must not look like an answer")

    def test_no_url_and_no_match_still_yields_nothing(self) -> None:
        failure = PinImportFailure.objects.create(
            profile=self.profile,
            cid=999,
            name="Unfindable Place",
            reason=PinImportFailureReason.LOOKUP_STALLED,
        )

        with mock.patch(f"{_GATEWAY}.search", return_value=[]):
            self.assertIsNone(guess_for_failure(failure))

    def test_an_undecodable_url_is_ignored(self) -> None:
        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[_osm("41.35", "-71.45")]),
            mock.patch(_S2, side_effect=ValueError("bad cell")),
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "address")

    def test_corroboration_does_not_get_stricter_at_high_latitude(self) -> None:
        """The agreement bound is a true distance, not a degree box.

        A degree of longitude shrinks with latitude - half its equatorial width at
        60 deg, a third at 70 - so a degree-based check silently tightened the
        further north the pin was. The same physical offset between the cell and
        the geocoded match must corroborate everywhere, or northern imports lose
        confidence for no geographic reason.
        """
        # One failure reused across latitudes: the S2 decode is mocked, so the
        # row's own url is irrelevant, and re-creating it would collide on cid.
        failure = self._failure("123 Main St")
        offset_deg_at_equator = 0.09  # ~10km, comfortably inside the 16km bound

        for latitude in (0.0, 42.0, 60.0, 70.0):
            with self.subTest(latitude=latitude):
                lng_offset = offset_deg_at_equator / max(math.cos(math.radians(latitude)), 1e-6)

                with (
                    mock.patch(f"{_GATEWAY}.search", return_value=[_osm(str(latitude), "0.0")]),
                    mock.patch(_S2, return_value=(latitude, lng_offset)),
                ):
                    guess = guess_for_failure(failure)

                self.assertEqual(guess.source, "address+area", f"lost corroboration at latitude {latitude}")

    def test_a_genuinely_distant_cell_still_fails_to_corroborate(self) -> None:
        """Guards the check above from passing because everything corroborates."""
        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", return_value=[_osm("60.0", "0.0")]),
            mock.patch(_S2, return_value=(60.0, 3.0)),  # ~167km east
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "address")

    def test_a_rate_limited_lookup_is_not_logged_as_an_error(self) -> None:
        """Nominatim's policy caps us at one call a minute and the queue reveals a
        card per scroll, so refusal is the common case, not an exceptional one.
        Logging a traceback per refused card buries the genuine geocoder failures
        in hundreds of expected ones."""
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", side_effect=RateLimitExceededError("nominatim")),
            mock.patch(_S2, return_value=(41.36, -71.46)),
            self.assertNoLogs("urbanlens.dashboard.services.pins.import_failure_guess", level="WARNING"),
        ):
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "area", "the decoded cell should still stand in")

    def test_a_genuine_geocoder_failure_is_still_logged(self) -> None:
        """Guards the check above from silencing real errors too."""
        failure = self._failure("123 Main St")

        with (
            mock.patch(f"{_GATEWAY}.search", side_effect=RuntimeError("boom")),
            mock.patch(_S2, return_value=(41.36, -71.46)),
            self.assertLogs("urbanlens.dashboard.services.pins.import_failure_guess", level="ERROR"),
        ):
            guess_for_failure(failure)

    def test_the_rate_limit_reaches_the_caller_through_the_real_gateway(self) -> None:
        """The earlier rate-limit test mocks ``NominatimGateway.search`` itself, so
        it proves the caller's handler works but says nothing about whether the
        exception can ever get there. ``search`` used to flatten every failure to
        ``[]``, which made that handler unreachable in production and the test
        green anyway. This one patches the *session*, so the exception has to
        travel the path it really travels."""
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        failure = self._failure("123 Main St")

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway.session",
                new_callable=mock.PropertyMock,
                create=True,
            ) as session,
            mock.patch(_S2, return_value=(41.36, -71.46)),
            self.assertNoLogs("urbanlens.dashboard.services.apis.locations.nominatim", level="ERROR"),
        ):
            session.return_value.get.side_effect = RateLimitExceededError("nominatim")
            guess = guess_for_failure(failure)

        self.assertEqual(guess.source, "area", "the decoded cell should still stand in")
