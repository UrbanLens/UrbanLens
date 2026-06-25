"""Additional Pin model tests covering methods not in test_pin_properties.py.

Covers:
- effective_color  (mock-based, badges M2M filtered by kind=tag)
- has_meaningful_name  (pure)
- rating  (DB, requires Review)
- add_category / change_category  (DB)
- to_json / to_detail_json  (DB)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin

# ── effective_color ────────────────────────────────────────────────────────────


class PinEffectiveColorTests(TestCase):
    """effective_color returns the color of the highest-order display badge that has one."""

    def _make_pin(self) -> Pin:
        pin = Pin()
        pin.nickname = None
        pin.icon = None
        pin.color = None
        return pin

    def _mock_badges(self, mock_badges: MagicMock, badges: list[MagicMock]) -> None:
        mock_badges.exclude.return_value.order_by.return_value = iter(badges)

    @patch.object(Pin, "badges")
    def test_returns_none_when_no_tags(self, mock_badges: MagicMock) -> None:
        self._mock_badges(mock_badges, [])
        self.assertIsNone(self._make_pin().effective_color)

    @patch.object(Pin, "badges")
    def test_returns_none_when_tags_have_no_color(self, mock_badges: MagicMock) -> None:
        tag = MagicMock()
        tag.effective_color = None
        tag.effective_icon = "star"
        tag.custom_icon = None
        tag.icon_is_overridden = False
        self._mock_badges(mock_badges, [tag])
        self.assertIsNone(self._make_pin().effective_color)

    @patch.object(Pin, "badges")
    def test_returns_first_tag_color(self, mock_badges: MagicMock) -> None:
        tag = MagicMock()
        tag.effective_color = "#ff0000"
        tag.effective_icon = "star"
        tag.custom_icon = None
        tag.icon_is_overridden = False
        self._mock_badges(mock_badges, [tag])
        self.assertEqual(self._make_pin().effective_color, "#ff0000")

    @patch.object(Pin, "badges")
    def test_skips_tag_without_color_uses_next(self, mock_badges: MagicMock) -> None:
        tag_no_color = MagicMock()
        tag_no_color.effective_color = None
        tag_no_color.effective_icon = "star"
        tag_no_color.custom_icon = None
        tag_no_color.icon_is_overridden = False
        tag_with_color = MagicMock()
        tag_with_color.effective_color = "#00ff00"
        tag_with_color.effective_icon = None
        tag_with_color.custom_icon = None
        tag_with_color.icon_is_overridden = False
        self._mock_badges(mock_badges, [tag_no_color, tag_with_color])
        self.assertEqual(self._make_pin().effective_color, "#00ff00")

    @patch.object(Pin, "badges")
    def test_first_tag_color_wins_over_later_tags(self, mock_badges: MagicMock) -> None:
        tag1 = MagicMock()
        tag1.effective_color = "#0000ff"
        tag1.effective_icon = "star"
        tag1.custom_icon = None
        tag1.icon_is_overridden = False
        tag2 = MagicMock()
        tag2.effective_color = "#ffffff"
        tag2.effective_icon = None
        tag2.custom_icon = None
        tag2.icon_is_overridden = False
        self._mock_badges(mock_badges, [tag1, tag2])
        self.assertEqual(self._make_pin().effective_color, "#0000ff")


# ── has_meaningful_name ────────────────────────────────────────────────────────

class PinHasMeaningfulNameTests(TestCase):
    """has_meaningful_name is False for Google placeholder names."""

    def _pin_with_name(self, nickname: str | None, loc_name: str = "") -> Pin:
        pin = Pin()
        pin.nickname = nickname
        loc_mock = MagicMock()
        loc_mock.name = loc_name
        pin._state.fields_cache["location"] = loc_mock
        return pin

    def _pin_no_location(self, nickname: str | None = None) -> Pin:
        pin = Pin()
        pin.nickname = nickname
        pin._state.fields_cache["location"] = None
        return pin

    def test_dropped_pin_is_not_meaningful(self) -> None:
        pin = self._pin_with_name(None, loc_name="Dropped pin")
        self.assertFalse(pin.has_meaningful_name)

    def test_no_information_available_is_not_meaningful(self) -> None:
        pin = self._pin_with_name(None, loc_name="No Information Available")
        self.assertFalse(pin.has_meaningful_name)

    def test_empty_string_is_not_meaningful(self) -> None:
        self.assertFalse(self._pin_no_location().has_meaningful_name)

    def test_real_name_is_meaningful(self) -> None:
        pin = self._pin_with_name("Old Warehouse")
        self.assertTrue(pin.has_meaningful_name)

    def test_nickname_meaningful_regardless_of_location(self) -> None:
        pin = self._pin_with_name("My Spot", loc_name="Dropped pin")
        self.assertTrue(pin.has_meaningful_name)


# ── rating ────────────────────────────────────────────────────────────────────

class PinRatingTests(TestCase):
    """rating returns the most recent review rating, or 0 when there are none."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)

    def test_no_reviews_returns_zero(self) -> None:
        self.assertEqual(self.pin.rating, 0)

    def test_single_review_returns_its_rating(self) -> None:
        baker.make("dashboard.Review", user=self.user, pin=self.pin, rating=4)
        self.pin = Pin.objects.get(pk=self.pin.pk)
        self.assertEqual(self.pin.rating, 4)

    def test_rating_is_integer(self) -> None:
        baker.make("dashboard.Review", user=self.user, pin=self.pin, rating=3)
        self.pin = Pin.objects.get(pk=self.pin.pk)
        self.assertIsInstance(self.pin.rating, int)


# ── add_category / change_category ────────────────────────────────────────────

class PinAddCategoryTests(TestCase):
    """add_category creates the Badge if needed and links it to the pin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="41.0", longitude="-73.0")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)

    def test_add_category_returns_a_badge(self) -> None:
        result = self.pin.add_category("factory")
        self.assertIsNotNone(result)

    def test_add_category_creates_badge_with_category_kind(self) -> None:
        from urbanlens.dashboard.models.badges.model import Badge
        self.pin.add_category("hospital")
        self.assertTrue(Badge.objects.filter(name="hospital", kind="category").exists())

    def test_add_category_links_badge_to_pin(self) -> None:
        self.pin.add_category("school")
        self.pin.refresh_from_db()
        names = list(self.pin.badges.filter(kind="category").values_list("name", flat=True))
        self.assertIn("school", names)

    def test_add_category_normalises_to_lowercase(self) -> None:
        result = self.pin.add_category("Prison")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "prison")


class PinChangeCategoryTests(TestCase):
    """change_category replaces all existing categories with the given one."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="42.0", longitude="-72.0")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)
        self.old_cat = baker.make("dashboard.Badge", name="old", kind="category", profile=None)
        self.new_cat = baker.make("dashboard.Badge", name="new_cat", kind="category", profile=None)
        self.pin.badges.add(self.old_cat)

    def test_change_category_sets_new_category(self) -> None:
        self.pin.change_category(self.new_cat.id)
        self.pin.refresh_from_db()
        self.assertIn(self.new_cat, self.pin.badges.filter(kind="category"))

    def test_change_category_removes_old_category(self) -> None:
        self.pin.change_category(self.new_cat.id)
        self.pin.refresh_from_db()
        self.assertNotIn(self.old_cat, self.pin.badges.filter(kind="category"))


# ── to_json / to_detail_json ──────────────────────────────────────────────────

class PinToJsonTests(TestCase):
    """to_json() serialises core pin fields to a dict."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make(
            "dashboard.Location", name="Steel Mill",
            latitude="40.000000", longitude="-74.000000",
        )
        self.pin = baker.make(
            Pin, profile=self.user.profile, location=self.location,
            nickname="My Steel Mill", priority=5,
        )

    def test_returns_dict(self) -> None:
        self.assertIsInstance(self.pin.to_json(), dict)

    def test_contains_name(self) -> None:
        self.assertIn("name", self.pin.to_json())

    def test_name_is_nickname_when_set(self) -> None:
        self.assertEqual(self.pin.to_json()["name"], "My Steel Mill")

    def test_contains_uuid(self) -> None:
        self.assertIn("uuid", self.pin.to_json())

    def test_contains_latitude(self) -> None:
        result = self.pin.to_json()
        self.assertIn("latitude", result)
        self.assertAlmostEqual(result["latitude"], 40.0, places=2)

    def test_contains_longitude(self) -> None:
        result = self.pin.to_json()
        self.assertIn("longitude", result)
        self.assertAlmostEqual(result["longitude"], -74.0, places=2)

    def test_contains_priority(self) -> None:
        self.assertEqual(self.pin.to_json()["priority"], 5)

    def test_statuses_is_a_list(self) -> None:
        self.assertIsInstance(self.pin.to_json()["statuses"], list)


class PinToDetailJsonTests(TestCase):
    """to_detail_json() serialises layout fields for detail-pin map markers."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make(
            "dashboard.Location", latitude="41.000000", longitude="-73.000000",
        )
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)

    def test_returns_dict(self) -> None:
        self.assertIsInstance(self.pin.to_detail_json(), dict)

    def test_contains_uuid(self) -> None:
        self.assertIn("uuid", self.pin.to_detail_json())

    def test_contains_pin_type(self) -> None:
        self.assertIn("pin_type", self.pin.to_detail_json())

    def test_contains_coordinates(self) -> None:
        result = self.pin.to_detail_json()
        self.assertIn("latitude", result)
        self.assertIn("longitude", result)
