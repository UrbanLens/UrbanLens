"""Tests for VisitQuerySet filter methods: for_pin, manual, from_takeout.

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


# ---------------------------------------------------------------------------
# manual
# ---------------------------------------------------------------------------

class VisitQuerySetManualTests(TestCase):
    """manual() returns only visits with source='manual'."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)

        self.manual_visit = _make_visit(self.pin, source=VisitSource.MANUAL)
        self.takeout_visit = _make_visit(self.pin, source=VisitSource.HISTORY)

    def test_manual_visit_is_included(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).manual()
        self.assertIn(self.manual_visit, qs)

    def test_takeout_visit_is_excluded(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).manual()
        self.assertNotIn(self.takeout_visit, qs)

    def test_manual_on_all_objects_returns_only_manual(self):
        qs = PinVisit.objects.manual()
        for visit in qs:
            self.assertEqual(visit.source, VisitSource.MANUAL)

    def test_manual_returns_correct_count(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).manual()
        self.assertEqual(qs.count(), 1)


# ---------------------------------------------------------------------------
# from_takeout
# ---------------------------------------------------------------------------

class VisitQuerySetFromTakeoutTests(TestCase):
    """from_takeout() returns only visits with source='history'."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)

        self.manual_visit = _make_visit(self.pin, source=VisitSource.MANUAL)
        self.takeout_visit = _make_visit(self.pin, source=VisitSource.HISTORY)

    def test_takeout_visit_is_included(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).from_takeout()
        self.assertIn(self.takeout_visit, qs)

    def test_manual_visit_is_excluded(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).from_takeout()
        self.assertNotIn(self.manual_visit, qs)

    def test_from_takeout_on_all_objects_returns_only_takeout(self):
        qs = PinVisit.objects.from_takeout()
        for visit in qs:
            self.assertEqual(visit.source, VisitSource.HISTORY)

    def test_from_takeout_returns_correct_count(self):
        qs = PinVisit.objects.for_pin(self.pin.pk).from_takeout()
        self.assertEqual(qs.count(), 1)

    def test_no_takeout_visits_returns_empty(self):
        pin2 = baker.make("dashboard.Pin", profile=self.profile)
        _make_visit(pin2, source=VisitSource.MANUAL)
        qs = PinVisit.objects.for_pin(pin2.pk).from_takeout()
        self.assertFalse(qs.exists())
