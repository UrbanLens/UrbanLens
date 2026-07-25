"""Tests for services.spotguessr.geo_bonus - country/state/city bonus scope and scoring."""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from django.contrib.gis.geos import Point
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
            mock_gateway.return_value.reverse_geocode_admin.return_value = {"country": "USA", "state": "New York", "city": "Albany"}
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, COUNTRY_BONUS + STATE_BONUS + CITY_BONUS)
        self.assertEqual(set(result.matched_tiers), {"country", "state", "city"})

    def test_a_disabled_tier_never_awards_points_even_if_it_matches(self) -> None:
        guess_point = Point(-73.75, 42.65, srid=4326)
        scope = BonusScope(country=False, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = {"country": "USA", "state": "New York", "city": "Albany"}
            result = bonus_points_for_guess(guess_point, self.location, scope)
        self.assertEqual(result.total, STATE_BONUS + CITY_BONUS)
        self.assertNotIn("country", result.matched_tiers)

    def test_no_match_at_all_earns_nothing(self) -> None:
        guess_point = Point(2.35, 48.85, srid=4326)
        scope = BonusScope(country=True, state=True, city=True)
        with patch("urbanlens.dashboard.services.spotguessr.geo_bonus.NominatimGateway") as mock_gateway:
            mock_gateway.return_value.reverse_geocode_admin.return_value = {"country": "France", "state": "Ile-de-France", "city": "Paris"}
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
