"""Tests for AddressableMixin address-component properties.

Tests use unsaved Location instances (no DB required) to exercise the
pure Python properties on AddressableMixin.
"""
from __future__ import annotations

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from urbanlens.dashboard.models.location.model import Location


# ── Strategies ─────────────────────────────────────────────────────────────────

_ascii_text = st.text(
	alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters='\n\r"'),
	min_size=1,
	max_size=40,
)
_opt_text = st.one_of(st.none(), _ascii_text)

_HYP = dict(max_examples=100, deadline=None)


def _loc(**kwargs) -> Location:
	"""Create an unsaved Location with address fields overridden from kwargs.

	Uses Location() so Django's __init__ sets up internal state correctly.
	No DB interaction occurs — save() is never called.
	"""
	loc = Location()
	for k, v in kwargs.items():
		setattr(loc, k, v)
	return loc


# ── address property ───────────────────────────────────────────────────────────

class AddressPropertyTests(unittest.TestCase):
	"""AddressableMixin.address builds from street_number, route, locality, state, zipcode."""

	def test_all_fields_present(self) -> None:
		loc = _loc(street_number="123", route="Main St", locality="Springfield",
			administrative_area_level_1="MA", zipcode="01234")
		self.assertEqual(loc.address, "123 Main St, Springfield, MA 01234")

	def test_returns_none_when_all_empty(self) -> None:
		loc = _loc()
		self.assertIsNone(loc.address)

	def test_street_number_without_route(self) -> None:
		loc = _loc(street_number="42")
		self.assertEqual(loc.address, "42")

	def test_route_appends_comma(self) -> None:
		loc = _loc(route="Elm Ave")
		self.assertIn("Elm Ave,", loc.address)

	def test_locality_appends_comma(self) -> None:
		loc = _loc(locality="Boston")
		self.assertIn("Boston,", loc.address)

	def test_state_and_zipcode_without_street(self) -> None:
		loc = _loc(administrative_area_level_1="NY", zipcode="10001")
		self.assertEqual(loc.address, "NY 10001")

	def test_zipcode_without_state(self) -> None:
		loc = _loc(zipcode="90210")
		self.assertEqual(loc.address, "90210")

	def test_state_without_zipcode(self) -> None:
		loc = _loc(administrative_area_level_1="CA")
		self.assertEqual(loc.address, "CA")

	@given(
		street_number=_opt_text,
		route=_opt_text,
		locality=_opt_text,
		state=_opt_text,
		zipcode=_opt_text,
	)
	@settings(**_HYP)
	def test_address_is_none_iff_all_components_are_none(
		self, street_number, route, locality, state, zipcode
	) -> None:
		loc = _loc(
			street_number=street_number,
			route=route,
			locality=locality,
			administrative_area_level_1=state,
			zipcode=zipcode,
		)
		all_empty = not any([street_number, route, locality, state, zipcode])
		if all_empty:
			self.assertIsNone(loc.address)
		else:
			self.assertIsNotNone(loc.address)

	@given(street_number=_ascii_text, route=_ascii_text)
	@settings(**_HYP)
	def test_address_contains_street_number_when_set(self, street_number, route) -> None:
		loc = _loc(street_number=street_number, route=route)
		self.assertIn(street_number, loc.address)

	@given(route=_ascii_text)
	@settings(**_HYP)
	def test_address_contains_route_when_set(self, route) -> None:
		loc = _loc(route=route)
		self.assertIn(route, loc.address)


# ── address_basic property ────────────────────────────────────────────────────

class AddressBasicPropertyTests(unittest.TestCase):
	"""AddressableMixin.address_basic — only street_number and route."""

	def test_both_fields_present(self) -> None:
		loc = _loc(street_number="10", route="Downing St")
		self.assertEqual(loc.address_basic, "10 Downing St")

	def test_street_number_only(self) -> None:
		loc = _loc(street_number="10")
		self.assertEqual(loc.address_basic, "10")

	def test_route_only(self) -> None:
		loc = _loc(route="Oak Rd")
		self.assertEqual(loc.address_basic, "Oak Rd")

	def test_neither_returns_none(self) -> None:
		loc = _loc()
		self.assertIsNone(loc.address_basic)

	def test_does_not_include_locality(self) -> None:
		loc = _loc(street_number="1", route="A St", locality="SomeCity")
		self.assertNotIn("SomeCity", loc.address_basic)

	@given(street_number=_ascii_text, route=_ascii_text)
	@settings(**_HYP)
	def test_both_components_present_in_result(self, street_number, route) -> None:
		loc = _loc(street_number=street_number, route=route)
		result = loc.address_basic
		self.assertIn(street_number, result)
		self.assertIn(route, result)

	@given(street_number=_opt_text, route=_opt_text)
	@settings(**_HYP)
	def test_basic_none_iff_both_none(self, street_number, route) -> None:
		loc = _loc(street_number=street_number, route=route)
		if street_number or route:
			self.assertIsNotNone(loc.address_basic)
		else:
			self.assertIsNone(loc.address_basic)


# ── address_extended property ─────────────────────────────────────────────────

class AddressExtendedPropertyTests(unittest.TestCase):
	"""AddressableMixin.address_extended — street with city, no state/zip."""

	def test_all_three_fields_present(self) -> None:
		loc = _loc(street_number="5", route="Penny Lane", locality="Liverpool")
		self.assertEqual(loc.address_extended, "5 Penny Lane, Liverpool")

	def test_route_appends_comma_before_locality(self) -> None:
		loc = _loc(route="Abbey Rd", locality="London")
		self.assertEqual(loc.address_extended, "Abbey Rd, London")

	def test_street_number_and_route_without_locality(self) -> None:
		loc = _loc(street_number="7", route="Baker St")
		self.assertEqual(loc.address_extended, "7 Baker St,")

	def test_none_when_all_empty(self) -> None:
		loc = _loc()
		self.assertIsNone(loc.address_extended)

	def test_does_not_include_state(self) -> None:
		loc = _loc(street_number="1", route="A St", locality="City", administrative_area_level_1="TX")
		self.assertNotIn("TX", loc.address_extended)

	@given(street_number=_opt_text, route=_opt_text, locality=_opt_text)
	@settings(**_HYP)
	def test_extended_none_iff_all_none(self, street_number, route, locality) -> None:
		loc = _loc(street_number=street_number, route=route, locality=locality)
		if street_number or route or locality:
			self.assertIsNotNone(loc.address_extended)
		else:
			self.assertIsNone(loc.address_extended)


# ── Proxy properties and setters ─────────────────────────────────────────────

class ProxyPropertyTests(unittest.TestCase):
	"""state, county, and city are thin proxies for administrative_area fields."""

	def test_state_getter_reads_level_1(self) -> None:
		loc = _loc(administrative_area_level_1="Texas")
		self.assertEqual(loc.state, "Texas")

	def test_state_setter_writes_level_1(self) -> None:
		loc = _loc()
		loc.state = "Florida"
		self.assertEqual(loc.administrative_area_level_1, "Florida")

	def test_county_getter_reads_level_2(self) -> None:
		loc = _loc(administrative_area_level_2="Travis County")
		self.assertEqual(loc.county, "Travis County")

	def test_county_setter_writes_level_2(self) -> None:
		loc = _loc()
		loc.county = "Kings County"
		self.assertEqual(loc.administrative_area_level_2, "Kings County")

	def test_city_getter_reads_locality(self) -> None:
		loc = _loc(locality="Austin")
		self.assertEqual(loc.city, "Austin")

	def test_city_setter_writes_locality(self) -> None:
		loc = _loc()
		loc.city = "Dallas"
		self.assertEqual(loc.locality, "Dallas")

	def test_state_returns_none_when_not_set(self) -> None:
		self.assertIsNone(_loc().state)

	def test_county_returns_none_when_not_set(self) -> None:
		self.assertIsNone(_loc().county)

	def test_city_returns_none_when_not_set(self) -> None:
		self.assertIsNone(_loc().city)

	@given(value=_ascii_text)
	@settings(**_HYP)
	def test_state_round_trips_through_setter(self, value: str) -> None:
		loc = _loc()
		loc.state = value
		self.assertEqual(loc.state, value)

	@given(value=_ascii_text)
	@settings(**_HYP)
	def test_city_round_trips_through_setter(self, value: str) -> None:
		loc = _loc()
		loc.city = value
		self.assertEqual(loc.city, value)

	@given(value=_ascii_text)
	@settings(**_HYP)
	def test_county_round_trips_through_setter(self, value: str) -> None:
		loc = _loc()
		loc.county = value
		self.assertEqual(loc.county, value)
