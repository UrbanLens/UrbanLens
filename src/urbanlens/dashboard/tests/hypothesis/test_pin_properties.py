"""Property-based tests for Pin model computed properties.

These tests exercise business logic that is expressed as Python properties on
Pin, using in-memory model instances - no database round-trips required.  Each
property tested here carries a real invariant that the rest of the application
depends on.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.tests.hypothesis.strategies import (
    latitude,
    longitude,
    nonempty_name,
    reasonable_date,
)

_FK_FIELDS: frozenset[str] = frozenset({"location"})


def _make_pin(**kwargs: Any) -> Pin:
    """Build an unsaved Pin with sensible defaults.

    Bypasses the database entirely; only the Python object is created so that
    pure-Python properties can be exercised in isolation.
    """
    defaults: dict[str, Any] = {
        "name": None,
        "location": None,
        "date_last_active": None,
        "date_abandoned": None,
        "icon": None,
        "color": None,
    }
    defaults.update(kwargs)
    pin = Pin()
    for k, v in defaults.items():
        if k in _FK_FIELDS:
            # Inject directly into the field cache to bypass the FK descriptor's
            # isinstance check - location is often a MagicMock in these tests.
            pin._state.fields_cache[k] = v
        else:
            setattr(pin, k, v)
    return pin


def _make_location(name: str, lat: Decimal | None = None, lon: Decimal | None = None) -> MagicMock:
    """Return a lightweight mock that quacks like a Location.

    ``display_name`` is what Pin.effective_name reads (the community wiki name,
    falling back to official_name); the mock exposes it directly.
    """
    loc = MagicMock()
    loc.display_name = name
    loc.official_name = name
    loc.latitude = lat if lat is not None else Decimal("42.0")
    loc.longitude = lon if lon is not None else Decimal("-73.0")
    return loc


# -- effective_name -------------------------------------------------------------

class PinEffectiveNameTests(TestCase):

    @given(nonempty_name)
    @settings(max_examples=300)
    def test_name_takes_priority_over_location_name(self, name: str) -> None:
        """When a custom pin name is set it must always win, regardless of the location."""
        loc = _make_location(name="Canonical Location")
        pin = _make_pin(name=name, location=loc)
        self.assertEqual(pin.effective_name, name)

    @given(nonempty_name)
    @settings(max_examples=300)
    def test_falls_back_to_location_name_when_name_is_none(self, location_name: str) -> None:
        loc = _make_location(name=location_name)
        pin = _make_pin(name=None, location=loc)
        self.assertEqual(pin.effective_name, location_name)

    @given(nonempty_name, nonempty_name)
    @settings(max_examples=200)
    def test_name_is_always_returned_verbatim(self, name: str, location_name: str) -> None:
        """The returned name must be exactly what was stored - no transformation."""
        loc = _make_location(name=location_name)
        pin = _make_pin(name=name, location=loc)
        self.assertIs(type(pin.effective_name), str)
        self.assertEqual(pin.effective_name, name)

    @given(st.one_of(st.just(""), st.just(None)))
    @settings(max_examples=50)
    def test_falsy_name_falls_through_to_location(self, name: str | None) -> None:
        """Empty string and None both trigger the location fallback."""
        loc = _make_location(name="Fallback Name")
        pin = _make_pin(name=name, location=loc)
        self.assertEqual(pin.effective_name, "Fallback Name")


# -- effective_latitude / effective_longitude -----------------------------------

class PinEffectiveCoordinateTests(TestCase):
    """effective_latitude/effective_longitude always proxy the linked Location.

    A Pin has no coordinate fields of its own (see AddressableModel) - there is
    no "pin override" concept anymore.
    """

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_effective_latitude_matches_location(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=lat, lon=lon)
        pin = _make_pin(location=loc)
        result = pin.effective_latitude
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result, float(lat), places=6)

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_effective_longitude_matches_location(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=lat, lon=lon)
        pin = _make_pin(location=loc)
        result = pin.effective_longitude
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result, float(lon), places=6)

    @given(latitude)
    @settings(max_examples=200)
    def test_effective_latitude_is_always_a_float(self, lat: Decimal) -> None:
        """Return type must be float, never Decimal."""
        loc = _make_location("Place", lat=lat, lon=Decimal("0"))
        pin = _make_pin(location=loc)
        result = pin.effective_latitude
        self.assertIsInstance(result, float)

    @given(longitude)
    @settings(max_examples=200)
    def test_effective_longitude_is_always_a_float(self, lon: Decimal) -> None:
        loc = _make_location("Place", lat=Decimal("0"), lon=lon)
        pin = _make_pin(location=loc)
        result = pin.effective_longitude
        self.assertIsInstance(result, float)


# -- effective_date_last_active -------------------------------------------------

class PinEffectiveDateLastActiveTests(TestCase):

    @given(reasonable_date)
    @settings(max_examples=300)
    def test_explicit_date_last_active_is_returned_unchanged(self, active_date: date) -> None:
        """When the field is set directly, it must be returned as-is."""
        pin = _make_pin(date_last_active=active_date, date_abandoned=None)
        self.assertEqual(pin.effective_date_last_active, active_date)

    @given(reasonable_date)
    @settings(max_examples=300)
    def test_inferred_from_date_abandoned_minus_one_day(self, abandoned: date) -> None:
        """When date_last_active is not set, activity is inferred as day before abandonment."""
        pin = _make_pin(date_last_active=None, date_abandoned=abandoned)
        expected = abandoned - timedelta(days=1)
        self.assertEqual(pin.effective_date_last_active, expected)

    def test_none_when_neither_date_is_set(self) -> None:
        pin = _make_pin(date_last_active=None, date_abandoned=None)
        self.assertIsNone(pin.effective_date_last_active)

    @given(reasonable_date, reasonable_date)
    @settings(max_examples=200)
    def test_explicit_date_wins_over_abandoned_inference(
        self,
        active_date: date,
        abandoned: date,
    ) -> None:
        """date_last_active always takes priority over the abandoned fallback."""
        pin = _make_pin(date_last_active=active_date, date_abandoned=abandoned)
        self.assertEqual(pin.effective_date_last_active, active_date)

    @given(reasonable_date)
    @settings(max_examples=200)
    def test_inferred_date_is_strictly_before_abandoned(self, abandoned: date) -> None:
        """The inferred activity date must be one day before abandonment - never equal or after."""
        pin = _make_pin(date_last_active=None, date_abandoned=abandoned)
        inferred = pin.effective_date_last_active
        self.assertIsNotNone(inferred)
        self.assertLess(inferred, abandoned)

    @given(reasonable_date)
    @settings(max_examples=200)
    def test_inferred_date_difference_is_exactly_one_day(self, abandoned: date) -> None:
        pin = _make_pin(date_last_active=None, date_abandoned=abandoned)
        inferred = pin.effective_date_last_active
        assert inferred is not None  # nosec B101
        self.assertEqual((abandoned - inferred).days, 1)


# -- effective_icon -------------------------------------------------------------

class PinEffectiveIconTests(TestCase):
    """effective_icon follows a defined priority chain.

    The DB-backed tag-lookup branch is not tested here (it requires a live ORM
    queryset) but the top two tiers - custom_icon and icon - are pure Python.
    """

    def _make_pin_with_icon(self, icon: str | None, custom_icon: Any = None) -> Pin:
        pin = _make_pin(icon=icon)
        # custom_icon is a Django ImageField; FileDescriptor.__set__ accepts assignment.
        mock_cf = MagicMock()
        mock_cf.__bool__ = lambda self: custom_icon is not None
        mock_cf.url = custom_icon or ""
        object.__setattr__(pin, "custom_icon", mock_cf if custom_icon else None)
        return pin

    @given(st.text(min_size=1, max_size=50, alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z"))))
    @settings(max_examples=200)
    def test_text_icon_field_is_returned_when_set(self, icon_key: str) -> None:
        """When only the icon CharField is set, it must be returned."""
        # effective_icon returns self.icon immediately - tags are never accessed.
        pin = self._make_pin_with_icon(icon=icon_key, custom_icon=None)
        self.assertEqual(pin.effective_icon, icon_key)

    def test_none_icon_returns_none_when_no_tags(self) -> None:
        pin = self._make_pin_with_icon(icon=None, custom_icon=None)
        mock_badges = MagicMock()
        mock_badges.exclude.return_value.order_by.return_value = []
        with patch.object(type(pin), "badges", new_callable=PropertyMock, return_value=mock_badges):
            self.assertIsNone(pin.effective_icon)

    @given(st.text(min_size=1, max_size=50, alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z"))))
    @settings(max_examples=100)
    def test_custom_icon_url_beats_icon_field(self, icon_key: str) -> None:
        """A custom uploaded icon takes priority over the text icon key."""
        # effective_icon returns self.custom_icon.url immediately - tags are never accessed.
        url = "/media/pin_custom_icons/test.png"
        pin = _make_pin(icon=icon_key)
        mock_cf = MagicMock()
        mock_cf.__bool__ = lambda self: True
        mock_cf.url = url
        object.__setattr__(pin, "custom_icon", mock_cf)
        self.assertEqual(pin.effective_icon, url)
