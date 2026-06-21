"""Tests for PinMarkupQuerySet filter methods: for_pin and for_profile.

All tests require the database - records are created with model_bakery.
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupType, PinMarkup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_markup(pin, profile, markup_type: str = MarkupType.LINE) -> PinMarkup:
    """Create a PinMarkup attached to the given pin and profile."""
    return baker.make(
        PinMarkup,
        parent_pin=pin,
        profile=profile,
        markup_type=markup_type,
        geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
    )


# ---------------------------------------------------------------------------
# for_pin (line 17)
# ---------------------------------------------------------------------------

class PinMarkupForPinTests(TestCase):
    """for_pin(pin) returns only markup items attached to that pin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")

        self.pin_a = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.pin_b = baker.make("dashboard.Pin", profile=self.profile, location=self.location)

        self.markup_a = _make_markup(self.pin_a, self.profile)
        self.markup_b = _make_markup(self.pin_b, self.profile)

    def test_returns_markup_for_given_pin(self):
        qs = PinMarkup.objects.for_pin(self.pin_a)
        self.assertIn(self.markup_a, qs)

    def test_excludes_markup_for_other_pin(self):
        qs = PinMarkup.objects.for_pin(self.pin_a)
        self.assertNotIn(self.markup_b, qs)

    def test_other_pin_returns_its_own_markup(self):
        qs = PinMarkup.objects.for_pin(self.pin_b)
        self.assertIn(self.markup_b, qs)

    def test_pin_with_no_markup_returns_empty_queryset(self):
        pin_c = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        qs = PinMarkup.objects.for_pin(pin_c)
        self.assertFalse(qs.exists())

    def test_multiple_markup_items_for_same_pin_all_returned(self):
        markup_a2 = _make_markup(self.pin_a, self.profile, markup_type=MarkupType.ARROW)
        qs = PinMarkup.objects.for_pin(self.pin_a)
        self.assertIn(self.markup_a, qs)
        self.assertIn(markup_a2, qs)
        self.assertEqual(qs.count(), 2)

    def test_result_is_chainable(self):
        # Should be able to chain further filters without error
        qs = PinMarkup.objects.for_pin(self.pin_a).filter(markup_type=MarkupType.LINE)
        self.assertIn(self.markup_a, qs)


# ---------------------------------------------------------------------------
# for_profile (line 21)
# ---------------------------------------------------------------------------

class PinMarkupForProfileTests(TestCase):
    """for_profile(profile) returns only markup items belonging to that profile."""

    def setUp(self):
        self.user_a = baker.make("auth.User")
        self.user_b = baker.make("auth.User")
        self.profile_a = self.user_a.profile
        self.profile_b = self.user_b.profile

        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile_a, location=self.location)

        self.markup_a = _make_markup(self.pin, self.profile_a)
        self.markup_b = _make_markup(self.pin, self.profile_b)

    def test_returns_markup_for_given_profile(self):
        qs = PinMarkup.objects.for_profile(self.profile_a)
        self.assertIn(self.markup_a, qs)

    def test_excludes_markup_for_other_profile(self):
        qs = PinMarkup.objects.for_profile(self.profile_a)
        self.assertNotIn(self.markup_b, qs)

    def test_other_profile_returns_its_own_markup(self):
        qs = PinMarkup.objects.for_profile(self.profile_b)
        self.assertIn(self.markup_b, qs)

    def test_profile_with_no_markup_returns_empty_queryset(self):
        user_c = baker.make("auth.User")
        qs = PinMarkup.objects.for_profile(user_c.profile)
        self.assertFalse(qs.exists())

    def test_multiple_markup_types_for_same_profile_all_returned(self):
        markup_a2 = _make_markup(self.pin, self.profile_a, markup_type=MarkupType.TEXT)
        qs = PinMarkup.objects.for_profile(self.profile_a)
        self.assertIn(self.markup_a, qs)
        self.assertIn(markup_a2, qs)

    def test_result_is_chainable(self):
        qs = PinMarkup.objects.for_profile(self.profile_a).filter(markup_type=MarkupType.LINE)
        self.assertIn(self.markup_a, qs)

    def test_for_pin_and_for_profile_can_be_chained(self):
        qs = PinMarkup.objects.for_pin(self.pin).for_profile(self.profile_a)
        self.assertIn(self.markup_a, qs)
        self.assertNotIn(self.markup_b, qs)
