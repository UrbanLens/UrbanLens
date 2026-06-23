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
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

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
        "nickname": None,
        "location": None,
        "latitude": None,
        "longitude": None,
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
    """Return a lightweight mock that quacks like a Location."""
    loc = MagicMock()
    loc.name = name
    loc.latitude = lat if lat is not None else Decimal("42.0")
    loc.longitude = lon if lon is not None else Decimal("-73.0")
    return loc


# ── effective_name ─────────────────────────────────────────────────────────────

class PinEffectiveNameTests(TestCase):

    @given(nonempty_name)
    @settings(max_examples=300)
    def test_nickname_takes_priority_over_location_name(self, nickname: str) -> None:
        """When a nickname is set it must always win, regardless of the location."""
        loc = _make_location(name="Canonical Location")
        pin = _make_pin(nickname=nickname, location=loc)
        self.assertEqual(pin.effective_name, nickname)

    @given(nonempty_name)
    @settings(max_examples=300)
    def test_falls_back_to_location_name_when_nickname_is_none(self, location_name: str) -> None:
        loc = _make_location(name=location_name)
        pin = _make_pin(nickname=None, location=loc)
        self.assertEqual(pin.effective_name, location_name)

    def test_empty_string_when_no_nickname_and_no_location(self) -> None:
        pin = _make_pin(nickname=None, location=None)
        self.assertEqual(pin.effective_name, "")

    @given(nonempty_name, nonempty_name)
    @settings(max_examples=200)
    def test_nickname_is_always_returned_verbatim(self, nickname: str, location_name: str) -> None:
        """The returned name must be exactly what was stored - no transformation."""
        loc = _make_location(name=location_name)
        pin = _make_pin(nickname=nickname, location=loc)
        self.assertIs(type(pin.effective_name), str)
        self.assertEqual(pin.effective_name, nickname)

    @given(st.one_of(st.just(""), st.just(None)))
    @settings(max_examples=50)
    def test_falsy_nickname_falls_through_to_location(self, nickname: str | None) -> None:
        """Empty string and None both trigger the location fallback."""
        loc = _make_location(name="Fallback Name")
        pin = _make_pin(nickname=nickname, location=loc)
        self.assertEqual(pin.effective_name, "Fallback Name")


# ── effective_latitude / effective_longitude ───────────────────────────────────

class PinEffectiveCoordinateTests(TestCase):

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_pin_override_latitude_takes_precedence(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=Decimal("0"), lon=Decimal("0"))
        pin = _make_pin(latitude=lat, longitude=lon, location=loc)
        result = pin.effective_latitude
        assert result is not None
        self.assertAlmostEqual(result, float(lat), places=6)

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_pin_override_longitude_takes_precedence(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=Decimal("0"), lon=Decimal("0"))
        pin = _make_pin(latitude=lat, longitude=lon, location=loc)
        result = pin.effective_longitude
        assert result is not None
        self.assertAlmostEqual(result, float(lon), places=6)

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_location_latitude_used_when_pin_has_no_override(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=lat, lon=lon)
        pin = _make_pin(latitude=None, longitude=None, location=loc)
        result = pin.effective_latitude
        assert result is not None
        self.assertAlmostEqual(result, float(lat), places=6)

    @given(latitude, longitude)
    @settings(max_examples=300)
    def test_location_longitude_used_when_pin_has_no_override(self, lat: Decimal, lon: Decimal) -> None:
        loc = _make_location("Place", lat=lat, lon=lon)
        pin = _make_pin(latitude=None, longitude=None, location=loc)
        result = pin.effective_longitude
        assert result is not None
        self.assertAlmostEqual(result, float(lon), places=6)

    def test_effective_latitude_is_none_when_no_override_and_no_location(self) -> None:
        pin = _make_pin(latitude=None, location=None)
        self.assertIsNone(pin.effective_latitude)

    def test_effective_longitude_is_none_when_no_override_and_no_location(self) -> None:
        pin = _make_pin(longitude=None, location=None)
        self.assertIsNone(pin.effective_longitude)

    @given(latitude, longitude, latitude, longitude)
    @settings(max_examples=150)
    def test_pin_override_always_beats_location(
        self,
        pin_lat: Decimal,
        pin_lon: Decimal,
        loc_lat: Decimal,
        loc_lon: Decimal,
    ) -> None:
        """As long as the pin has its own coordinates, location coords are irrelevant."""
        loc = _make_location("Place", lat=loc_lat, lon=loc_lon)
        pin = _make_pin(latitude=pin_lat, longitude=pin_lon, location=loc)
        eff_lat = pin.effective_latitude
        eff_lon = pin.effective_longitude
        assert eff_lat is not None
        assert eff_lon is not None
        self.assertAlmostEqual(eff_lat, float(pin_lat), places=6)
        self.assertAlmostEqual(eff_lon, float(pin_lon), places=6)

    @given(latitude)
    @settings(max_examples=200)
    def test_effective_latitude_is_always_a_float_or_none(self, lat: Decimal) -> None:
        """Return type must be float (or None), never Decimal."""
        pin = _make_pin(latitude=lat, location=None)
        result = pin.effective_latitude
        self.assertIsInstance(result, float)

    @given(longitude)
    @settings(max_examples=200)
    def test_effective_longitude_is_always_a_float_or_none(self, lon: Decimal) -> None:
        pin = _make_pin(longitude=lon, location=None)
        result = pin.effective_longitude
        self.assertIsInstance(result, float)


# ── effective_date_last_active ─────────────────────────────────────────────────

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
        assert inferred is not None
        self.assertEqual((abandoned - inferred).days, 1)


# ── effective_icon ─────────────────────────────────────────────────────────────

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

    @given(st.text(min_size=1, max_size=50, alphabet=st.characters(min_codepoint=ord('a'), max_codepoint=ord('z'))))
    @settings(max_examples=200)
    def test_text_icon_field_is_returned_when_set(self, icon_key: str) -> None:
        """When only the icon CharField is set, it must be returned."""
        # effective_icon returns self.icon immediately - tags are never accessed.
        pin = self._make_pin_with_icon(icon=icon_key, custom_icon=None)
        self.assertEqual(pin.effective_icon, icon_key)

    @patch.object(Pin, "tags")
    def test_none_icon_returns_none_when_no_tags(self, mock_tags: MagicMock) -> None:
        # Patch at class level: ManyToManyDescriptor.__set__ rejects instance assignment.
        mock_tags.order_by.return_value = iter([])
        pin = self._make_pin_with_icon(icon=None, custom_icon=None)
        self.assertIsNone(pin.effective_icon)

    @given(st.text(min_size=1, max_size=50, alphabet=st.characters(min_codepoint=ord('a'), max_codepoint=ord('z'))))
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


# ── location proxy properties ──────────────────────────────────────────────────

class PinLocationProxyTests(TestCase):
    """The address proxy properties simply delegate to the linked Location."""

    _PROXY_ATTRS = ("place_name", "address", "address_basic", "address_extended",
                    "state", "county", "city", "country", "cached_place_name")

    @given(st.sampled_from(_PROXY_ATTRS))
    @settings(max_examples=200)
    def test_proxy_returns_none_when_location_is_none(self, attr: str) -> None:
        pin = _make_pin(location=None)
        self.assertIsNone(getattr(pin, attr))

    @given(st.sampled_from(_PROXY_ATTRS), st.text(min_size=0, max_size=255))
    @settings(max_examples=200)
    def test_proxy_delegates_to_location_attribute(self, attr: str, value: str) -> None:
        loc = MagicMock()
        setattr(loc, attr, value)
        pin = _make_pin(location=loc)
        self.assertEqual(getattr(pin, attr), value)

    def test_has_place_name_false_when_no_location(self) -> None:
        pin = _make_pin(location=None)
        self.assertFalse(pin.has_place_name())

    def test_has_place_name_delegates_to_location(self) -> None:
        loc = MagicMock()
        loc.has_place_name.return_value = True
        pin = _make_pin(location=loc)
        self.assertTrue(pin.has_place_name())
        loc.has_place_name.assert_called_once()
