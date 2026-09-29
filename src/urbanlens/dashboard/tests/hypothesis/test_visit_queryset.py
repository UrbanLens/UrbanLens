"""Tests for VisitQuerySet.for_pin.

All tests require the database - records are created with model_bakery.
"""

from __future__ import annotations

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_visit(pin, source: str = VisitSource.MANUAL) -> PinVisit:
    """Create a PinVisit for the given pin with an explicit source."""
    return baker.make(PinVisit, pin=pin, source=source, visited_at=timezone.now())


# ---------------------------------------------------------------------------
# for_pin
# ---------------------------------------------------------------------------


class VisitQuerySetForPinTests(TestCase):
    """for_pin(pin_id) returns only visits belonging to that pin."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin_a = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        # A profile may hold only one root pin per Location, so pin_b gets its own.
        self.pin_b = baker.make("dashboard.Pin", profile=self.profile)

        self.visit_a = _make_visit(self.pin_a)
        self.visit_b = _make_visit(self.pin_b)

    def test_returns_visit_for_correct_pin(self):
        qs = PinVisit.objects.for_pin(self.pin_a.pk)
        self.assertIn(self.visit_a, qs)

    def test_excludes_visit_for_other_pin(self):
        qs = PinVisit.objects.for_pin(self.pin_a.pk)
        self.assertNotIn(self.visit_b, qs)

    def test_other_pin_returns_its_own_visit(self):
        qs = PinVisit.objects.for_pin(self.pin_b.pk)
        self.assertIn(self.visit_b, qs)

    def test_nonexistent_pin_id_returns_empty_queryset(self):
        qs = PinVisit.objects.for_pin(999999)
        self.assertFalse(qs.exists())

    def test_returns_queryset_type(self):
        qs = PinVisit.objects.for_pin(self.pin_a.pk)
        # Should be chainable - filter further without error
        self.assertFalse(qs.filter(source="nonexistent").exists())

    def test_multiple_visits_for_same_pin_all_returned(self):
        visit_a2 = _make_visit(self.pin_a)
        qs = PinVisit.objects.for_pin(self.pin_a.pk)
        self.assertIn(self.visit_a, qs)
        self.assertIn(visit_a2, qs)
        self.assertEqual(qs.count(), 2)
