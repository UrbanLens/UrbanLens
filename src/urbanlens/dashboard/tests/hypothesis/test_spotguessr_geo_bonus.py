"""Tests for services.spotguessr.geo_bonus - country/state/city bonus scope and scoring."""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.spotguessr.geo_bonus import (
    CITY_BONUS,
    COUNTRY_BONUS,
    STATE_BONUS,
    BonusScope,
    bonus_points_for_guess,
    bonus_scope_for,
)

_coordinate_counter = count()


def _make_location(**kwargs) -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}", **kwargs)


class BonusScopeForTests(TestCase):
    def test_a_single_shared_country_disables_the_country_tier_only(self) -> None:
        _make_location(country="USA", administrative_area_level_1="NY", locality="Albany")
        _make_location(country="USA", administrative_area_level_1="CA", locality="Fresno")
        scope = bonus_scope_for(Location.objects.all())
        self.assertFalse(scope.country)
        self.assertTrue(scope.state)
        self.assertTrue(scope.city)

    def test_a_single_shared_state_disables_country_and_state_tiers(self) -> None:
        _make_location(country="USA", administrative_area_level_1="NY", locality="Albany")
        _make_location(country="USA", administrative_area_level_1="NY", locality="Buffalo")
        scope = bonus_scope_for(Location.objects.all())
        self.assertFalse(scope.country)
        self.assertFalse(scope.state)
        self.assertTrue(scope.city)

    def test_all_shared_disables_every_tier(self) -> None:
        _make_location(country="USA", administrative_area_level_1="NY", locality="Albany")
        _make_location(country="USA", administrative_area_level_1="NY", locality="Albany")
        scope = bonus_scope_for(Location.objects.all())
        self.assertFalse(scope.country)
        self.assertFalse(scope.state)
        self.assertFalse(scope.city)

    def test_multiple_countries_enables_every_tier(self) -> None:
        _make_location(country="USA", administrative_area_level_1="NY", locality="Albany")
        _make_location(country="Canada", administrative_area_level_1="ON", locality="Toronto")
        scope = bonus_scope_for(Location.objects.all())
        self.assertTrue(scope.country)
        self.assertTrue(scope.state)
        self.assertTrue(scope.city)


class BonusPointsForGuessTests(SimpleTestCase):
    def setUp(self) -> None:
        """Reset shared state before each guess so the tests stay order-independent.

        ``_reverse_geocode_admin_cached`` memoizes into the Django cache keyed by rounded coordinates, and the
        cache is process-wide rather than per-test."""
        cache.clear()
        self.location = Location(country="USA", administrative_area_level_1="New York", locality="Albany")

    def test_no_tiers_offered_skips_the_geocode_call_entirely(self) -> None:
        guess_point = Point(-73.75, 42.65, srid=4326)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            result = bonus_points_for_guess(guess_point, self.location, BonusScope())
        mock_gateway.assert_not_called()
        self.assertEqual(result.total, 0)

    def test_matching_every_offered_tier_stacks_the_bonus(self) -> None:
        guess_point = Point(-73.75, 42.65, srid=4326)
        scope = BonusScope(country=True, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = {
                "country": "USA",
                "state": "New York",
                "city": "Albany",
            }
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, COUNTRY_BONUS + STATE_BONUS + CITY_BONUS)
        self.assertEqual(set(result.matched_tiers), {"country", "state", "city"})

    def test_a_disabled_tier_never_awards_points_even_if_it_matches(self) -> None:
        guess_point = Point(-73.75, 42.65, srid=4326)
        scope = BonusScope(country=False, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = {
                "country": "USA",
                "state": "New York",
                "city": "Albany",
            }
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, STATE_BONUS + CITY_BONUS)
        self.assertNotIn("country", result.matched_tiers)

    def test_no_match_at_all_earns_nothing(self) -> None:
        guess_point = Point(2.35, 48.85, srid=4326)
        scope = BonusScope(country=True, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = {
                "country": "France",
                "state": "Ile-de-France",
                "city": "Paris",
            }
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, 0)
        self.assertEqual(result.matched_tiers, [])

    def test_geocode_failure_earns_nothing_without_raising(self) -> None:
        guess_point = Point(-73.75, 42.65, srid=4326)
        scope = BonusScope(country=True, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = None
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, 0)


class CanonicalSpellingTests(SimpleTestCase):
    """Google stores a US state as "NY" and the country as "United States"; Nominatim answers "New York"."""

    def setUp(self) -> None:
        cache.clear()
        self.scope = BonusScope(country=True, state=True, city=True)

    def _score(self, location: Location, admin: dict[str, str]):
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = admin
            return bonus_points_for_guess(Point(-73.75, 42.65, srid=4326), location, self.scope)

    def test_a_state_abbreviation_matches_its_full_name(self) -> None:
        location = Location(country="United States", administrative_area_level_1="NY", locality="Albany")
        result = self._score(location, {"country": "United States", "state": "New York", "city": "Albany"})
        self.assertEqual(set(result.matched_tiers), {"country", "state", "city"})

    def test_usa_spellings_match_each_other(self) -> None:
        location = Location(country="USA", administrative_area_level_1="NY", locality="Albany")
        result = self._score(location, {"country": "United States of America", "state": "New York", "city": "Albany"})
        self.assertIn("country", result.matched_tiers)

    def test_a_different_state_still_does_not_match(self) -> None:
        location = Location(country="United States", administrative_area_level_1="NY", locality="Albany")
        result = self._score(location, {"country": "United States", "state": "New Jersey", "city": "Newark"})
        self.assertEqual(result.matched_tiers, ["country"])

    def test_two_unknown_states_are_not_a_match(self) -> None:
        location = Location(country="United States", administrative_area_level_1="", locality="")
        result = self._score(location, {"country": "", "state": "", "city": ""})
        self.assertEqual(result.matched_tiers, [])


class CanonicalStateTextTests(SimpleTestCase):
    def test_accents_and_punctuation_do_not_split_one_region(self) -> None:
        from urbanlens.dashboard.services.locations.naming import canonical_state

        self.assertEqual(canonical_state("Île-de-France"), canonical_state("Ile de France"))

    def test_a_non_latin_name_keeps_its_letters(self) -> None:
        from urbanlens.dashboard.services.locations.naming import canonical_state

        self.assertNotEqual(canonical_state("東京都"), canonical_state("大阪府"))


class CanonicalScopeTests(TestCase):
    def test_two_spellings_of_one_state_are_one_state(self) -> None:
        _make_location(country="United States", administrative_area_level_1="NY", locality="Albany")
        _make_location(country="USA", administrative_area_level_1="New York", locality="Buffalo")
        scope = bonus_scope_for(Location.objects.all())
        self.assertFalse(scope.country)
        self.assertFalse(scope.state)


class NominatimLanguageTests(SimpleTestCase):
    def test_the_admin_lookup_asks_for_english_names(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        gateway = NominatimGateway.__new__(NominatimGateway)
        gateway.base_url = "https://nominatim.example"
        with patch.object(NominatimGateway, "session", create=True) as session:
            session.get.return_value.json.return_value = {"address": {"country": "Deutschland"}}
            gateway.reverse_geocode_admin(52.5, 13.4)
        self.assertEqual(session.get.call_args.kwargs["params"]["accept-language"], "en")
