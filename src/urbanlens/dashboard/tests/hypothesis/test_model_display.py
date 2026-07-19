"""Tests for __str__ and to_json() on models not covered elsewhere.

Covers:
- PinVisit.__str__ and VisitSource enum values
- PinMarkup.__str__, to_json(), and MarkupType enum values
- PinAlias.__str__ and LocationAlias.__str__

All tests that rely on DB access use django.test.TestCase (with baker); pure
property/display tests use unittest.TestCase with unsaved instances.
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

from django.db import IntegrityError, transaction
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.markup.model import MarkupType, PinMarkup
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

_MARKUP_TYPES = list(MarkupType.values)
_VISIT_SOURCES = list(VisitSource.values)


# -- PinVisit ------------------------------------------------------------------

class PinVisitStrTests(SimpleTestCase):
    """PinVisit.__str__ contains the pin_id and a YYYY-MM-DD date."""

    def _visit(self, pin_id: int, year: int, month: int, day: int) -> PinVisit:
        v = PinVisit()
        v.pin_id = pin_id
        v.visited_at = datetime(year, month, day, 10, 30, tzinfo=UTC)
        return v

    def test_str_contains_pin_id(self) -> None:
        self.assertIn("42", str(self._visit(42, 2024, 3, 15)))

    def test_str_contains_date(self) -> None:
        self.assertIn("2024-03-15", str(self._visit(7, 2024, 3, 15)))

    def test_str_contains_word_visit(self) -> None:
        result = str(self._visit(1, 2020, 1, 1))
        self.assertIn("Visit", result)

    @given(
        st.integers(min_value=1, max_value=9999),
        st.dates(min_value=datetime(2000, 1, 1, tzinfo=UTC).date(),
                 max_value=datetime(2099, 12, 31, tzinfo=UTC).date()),
    )
    @settings(max_examples=50, deadline=None)
    def test_str_always_contains_pin_id_and_date(self, pin_id: int, d) -> None:
        v = PinVisit()
        v.pin_id = pin_id
        v.visited_at = datetime(d.year, d.month, d.day, tzinfo=UTC)
        s = str(v)
        self.assertIn(str(pin_id), s)
        self.assertIn(f"{d.year:04d}-{d.month:02d}-{d.day:02d}", s)


class VisitSourceEnumTests(SimpleTestCase):
    """VisitSource has the expected members and values."""

    def test_manual_value(self) -> None:
        self.assertEqual(VisitSource.MANUAL.value, "manual")

    def test_history_value(self) -> None:
        self.assertEqual(VisitSource.HISTORY.value, "history")

    def test_trip_value(self) -> None:
        self.assertEqual(VisitSource.TRIP.value, "trip")

    def test_user_value(self) -> None:
        self.assertEqual(VisitSource.USER.value, "user")

    def test_photo_value(self) -> None:
        self.assertEqual(VisitSource.PHOTO.value, "photo")

    def test_geolocation_value(self) -> None:
        self.assertEqual(VisitSource.GEOLOCATION.value, "geolocation")

    def test_safety_checkin_value(self) -> None:
        self.assertEqual(VisitSource.SAFETY_CHECKIN.value, "safety_checkin")

    def test_exactly_seven_members(self) -> None:
        self.assertEqual(len(VisitSource.values), 7)


# -- PinMarkup -----------------------------------------------------------------

class PinMarkupStrTests(TestCase):
    """PinMarkup.__str__ encodes the markup_type and parent pin id."""

    def _make_markup(self, markup_type: str, label: str) -> PinMarkup:
        user = baker.make("auth.User")
        location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        pin = baker.make("dashboard.Pin", profile=user.profile, location=location)
        return baker.make(
            "dashboard.PinMarkup",
            parent_pin=pin,
            profile=user.profile,
            markup_type=markup_type,
            label=label,
        )

    def test_str_contains_markup_type(self) -> None:
        markup = self._make_markup("line", "my line")
        self.assertIn("line", str(markup))

    def test_str_contains_label_when_set(self) -> None:
        markup = self._make_markup("text", "entrance")
        self.assertIn("entrance", str(markup))

    def test_str_shows_unlabelled_when_empty_label(self) -> None:
        markup = self._make_markup("arrow", "")
        self.assertIn("unlabelled", str(markup))

    def test_str_contains_parent_pin_id(self) -> None:
        markup = self._make_markup("circle", "fence")
        self.assertIn(str(markup.parent_pin_id), str(markup))


class PinMarkupToJsonTests(TestCase):
    """PinMarkup.to_json() returns all required fields for Leaflet rendering."""

    def setUp(self):
        user = baker.make("auth.User")
        location = baker.make("dashboard.Location", latitude="41.0", longitude="-73.0")
        pin = baker.make("dashboard.Pin", profile=user.profile, location=location)
        self.markup = baker.make(
            "dashboard.PinMarkup",
            parent_pin=pin,
            profile=user.profile,
            markup_type="polygon",
            geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            label="test zone",
            color="#ff0000",
            stroke_width=4,
            border_color="#000000",
            fill_opacity=80,
            border_opacity=100,
        )

    def test_returns_dict(self) -> None:
        self.assertIsInstance(self.markup.to_json(), dict)

    def test_contains_uuid(self) -> None:
        self.assertIn("uuid", self.markup.to_json())

    def test_contains_markup_type(self) -> None:
        self.assertEqual(self.markup.to_json()["markup_type"], "polygon")

    def test_contains_geometry(self) -> None:
        self.assertIn("geometry", self.markup.to_json())
        self.assertEqual(self.markup.to_json()["geometry"]["type"], "Polygon")

    def test_contains_label(self) -> None:
        self.assertEqual(self.markup.to_json()["label"], "test zone")

    def test_contains_color(self) -> None:
        self.assertEqual(self.markup.to_json()["color"], "#ff0000")

    def test_contains_stroke_width(self) -> None:
        self.assertEqual(self.markup.to_json()["stroke_width"], 4)

    def test_contains_border_color(self) -> None:
        self.assertEqual(self.markup.to_json()["border_color"], "#000000")

    def test_contains_fill_opacity(self) -> None:
        self.assertEqual(self.markup.to_json()["fill_opacity"], 80)

    def test_contains_border_opacity(self) -> None:
        self.assertEqual(self.markup.to_json()["border_opacity"], 100)

    def test_uuid_is_string(self) -> None:
        self.assertIsInstance(self.markup.to_json()["uuid"], str)

    def test_all_required_keys_present(self) -> None:
        required = {"uuid", "markup_type", "geometry", "label", "color", "stroke_width", "border_color", "fill_opacity", "border_opacity", "security_indicator"}
        self.assertEqual(required, set(self.markup.to_json()))


class MarkupTypeEnumTests(SimpleTestCase):
    """MarkupType has the expected set of visual annotation kinds."""

    def test_has_line(self) -> None:
        self.assertIn("line", MarkupType.values)

    def test_has_arrow(self) -> None:
        self.assertIn("arrow", MarkupType.values)

    def test_has_text(self) -> None:
        self.assertIn("text", MarkupType.values)

    def test_has_square(self) -> None:
        self.assertIn("square", MarkupType.values)

    def test_has_circle(self) -> None:
        self.assertIn("circle", MarkupType.values)

    def test_has_polygon(self) -> None:
        self.assertIn("polygon", MarkupType.values)

    def test_has_pin(self) -> None:
        self.assertIn("pin", MarkupType.values)

    def test_exactly_seven_members(self) -> None:
        self.assertEqual(len(MarkupType.values), 7)

    @given(st.sampled_from(_MARKUP_TYPES))
    @settings(max_examples=50, deadline=None)
    def test_every_member_has_a_label(self, value: str) -> None:
        member = MarkupType(value)
        self.assertTrue(member.label)


# -- Alias models --------------------------------------------------------------

class PinAliasStrTests(TestCase):
    """PinAlias.__str__ includes the alias name and the phrase 'pin alias'."""

    def test_str_contains_name(self) -> None:
        user = baker.make("auth.User")
        location = baker.make("dashboard.Location", latitude="42.0", longitude="-72.0")
        pin = baker.make("dashboard.Pin", profile=user.profile, location=location)
        alias = baker.make("dashboard.PinAlias", pin=pin, name="Westy Side")
        self.assertIn("Westy Side", str(alias))

    def test_str_contains_pin_alias(self) -> None:
        user = baker.make("auth.User")
        location = baker.make("dashboard.Location", latitude="43.0", longitude="-71.0")
        pin = baker.make("dashboard.Pin", profile=user.profile, location=location)
        alias = baker.make("dashboard.PinAlias", pin=pin, name="Old Mill")
        self.assertIn("pin alias", str(alias))


class WikiAliasStrTests(TestCase):
    """WikiAlias.__str__ includes the alias name and 'wiki alias'."""

    def test_str_contains_name(self) -> None:
        wiki = baker.make("dashboard.Wiki")
        alias = baker.make("dashboard.WikiAlias", wiki=wiki, name="The Ruin")
        self.assertIn("The Ruin", str(alias))

    def test_str_contains_wiki_alias(self) -> None:
        wiki = baker.make("dashboard.Wiki")
        alias = baker.make("dashboard.WikiAlias", wiki=wiki, name="Forgotten Mill")
        self.assertIn("wiki alias", str(alias))

    def test_created_by_is_optional(self) -> None:
        """WikiAlias can be created without a profile (created_by=None)."""
        wiki = baker.make("dashboard.Wiki")
        alias = baker.make("dashboard.WikiAlias", wiki=wiki, name="Anonymous", created_by=None)
        self.assertIsNone(alias.created_by)
        self.assertIn("Anonymous", str(alias))


class PinAliasUniquenessTests(TestCase):
    """Duplicate alias names on the same pin are rejected."""

    def test_duplicate_alias_name_raises(self) -> None:
        user = baker.make("auth.User")
        location = baker.make("dashboard.Location", latitude="47.0", longitude="-67.0")
        pin = baker.make("dashboard.Pin", profile=user.profile, location=location)
        baker.make("dashboard.PinAlias", pin=pin, name="Tunnel Entrance")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PinAlias.objects.create(pin=pin, name="Tunnel Entrance")

    def test_same_name_on_different_pins_is_allowed(self) -> None:
        user = baker.make("auth.User")
        location_a = baker.make("dashboard.Location", latitude="48.0", longitude="-66.0")
        location_b = baker.make("dashboard.Location", latitude="48.500000", longitude="-66.500000")
        pin_a = baker.make("dashboard.Pin", profile=user.profile, location=location_a)
        pin_b = baker.make("dashboard.Pin", profile=user.profile, location=location_b)
        baker.make("dashboard.PinAlias", pin=pin_a, name="Side Door")
        alias_b = baker.make("dashboard.PinAlias", pin=pin_b, name="Side Door")
        self.assertEqual(alias_b.name, "Side Door")
