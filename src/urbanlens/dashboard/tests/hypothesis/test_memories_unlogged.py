"""Tests for the Memories "log your visits" flow.

Covers the queryset that finds pins marked visited without a dated record
(``PinQuerySet.visited_without_record``), the service that surfaces them
(``unlogged_visited_pins``), and the view that logs/edits those visits
(``MemoriesVisitView``). All require the database because a Pin's coordinates
live on its linked PostGIS-backed Location.
"""

from __future__ import annotations

import datetime
import itertools

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _aware(year: int, month: int, day: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(year, month, day, 12, 0, 0))


def _make_pin(profile, *, last_visited=None, name=None, parent_pin=None) -> Pin:
    """Create a test pin with a uniquely-located Location to dodge the unique constraint."""
    offset = next(_COORDS)
    location = baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}")
    return baker.make("dashboard.Pin", profile=profile, location=location, last_visited=last_visited, name=name, parent_pin=parent_pin)


class VisitedWithoutRecordQuerySetTests(TestCase):
    """PinQuerySet.visited_without_record() surfaces only marked-but-unlogged root pins."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def _unlogged(self):
        return Pin.objects.filter(profile=self.profile).visited_without_record()

    def test_pin_with_last_visited_and_no_record_is_included(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        self.assertIn(pin, self._unlogged())

    def test_pin_with_visit_record_is_excluded(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), source=VisitSource.MANUAL)
        self.assertNotIn(pin, self._unlogged())

    def test_never_visited_pin_is_excluded(self) -> None:
        pin = _make_pin(self.profile, last_visited=None)
        self.assertNotIn(pin, self._unlogged())

    def test_pin_with_visited_badge_but_no_record_is_included(self) -> None:
        pin = _make_pin(self.profile, last_visited=None)
        badge = baker.make("dashboard.Badge", profile=self.profile, kind="status", name="Visited")
        pin.badges.add(badge)
        self.assertIn(pin, self._unlogged())

    def test_detail_pin_is_excluded(self) -> None:
        parent = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        child = _make_pin(self.profile, last_visited=_aware(2024, 6, 1), parent_pin=parent)
        self.assertNotIn(child, self._unlogged())

    def test_dismissed_pin_is_excluded(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        pin.unlogged_visit_dismissed = True
        pin.save(update_fields=["unlogged_visit_dismissed"])
        self.assertNotIn(pin, self._unlogged())


class UnloggedVisitedPinsServiceTests(TestCase):
    """unlogged_visited_pins() scopes to the profile, orders by recency, and respects the limit."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.other = baker.make(User).profile

    def test_only_returns_owning_profiles_pins(self) -> None:
        mine = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        theirs = _make_pin(self.other, last_visited=_aware(2024, 6, 1))
        result = unlogged_visited_pins(self.profile)
        self.assertIn(mine, result)
        self.assertNotIn(theirs, result)

    def test_orders_most_recently_visited_first(self) -> None:
        older = _make_pin(self.profile, last_visited=_aware(2024, 1, 1))
        newer = _make_pin(self.profile, last_visited=_aware(2024, 12, 1))
        self.assertEqual(unlogged_visited_pins(self.profile), [newer, older])

    def test_respects_limit(self) -> None:
        for month in range(1, 6):
            _make_pin(self.profile, last_visited=_aware(2024, month, 1))
        self.assertEqual(len(unlogged_visited_pins(self.profile, limit=3)), 3)


class MemoriesVisitViewTests(TestCase):
    """MemoriesVisitView logs a dated visit for an unlogged pin and edits existing ones."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")

    def test_get_add_form_prefills_last_visited_date(self) -> None:
        response = self.client.get(reverse("memories.visit", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["default_date"], "2024-06-01")

    def test_post_creates_visit_and_drops_pin_from_band(self) -> None:
        response = self.client.post(reverse("memories.visit", args=[self.pin.slug]), {"visited_date": "2024-06-15"})

        self.assertEqual(response.status_code, 200)
        visit = PinVisit.objects.get(pin=self.pin)
        self.assertEqual(visit.visited_at.date(), datetime.date(2024, 6, 15))
        self.assertEqual(visit.source, VisitSource.MANUAL)
        self.assertNotIn(self.pin, response.context["unlogged_visits"])
        self.assertIn("memoriesFeedRefresh", response["HX-Trigger"])

    def test_post_without_date_is_rejected(self) -> None:
        response = self.client.post(reverse("memories.visit", args=[self.pin.slug]), {})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())

    def test_post_edit_updates_existing_visit(self) -> None:
        visit = PinVisit.objects.create(pin=self.pin, visited_at=_aware(2024, 6, 1), source=VisitSource.MANUAL)

        response = self.client.post(
            reverse("memories.visit.edit", args=[self.pin.slug, visit.id]),
            {"visited_date": "2024-07-04", "notes": "Great light"},
        )

        self.assertEqual(response.status_code, 200)
        visit.refresh_from_db()
        self.assertEqual(visit.visited_at.date(), datetime.date(2024, 7, 4))
        self.assertEqual(visit.notes, "Great light")

    def test_cannot_log_visit_on_another_users_pin(self) -> None:
        other = baker.make(User)
        other_pin = _make_pin(other.profile, last_visited=_aware(2024, 6, 1), name="Their Secret Spot")

        response = self.client.post(reverse("memories.visit", args=[other_pin.slug]), {"visited_date": "2024-06-15"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinVisit.objects.filter(pin=other_pin).exists())


class MemoriesVisitsViewTests(TestCase):
    """MemoriesVisitsView (the Visits subpage) renders the unlogged-visits list."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_lists_unlogged_pins(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")
        response = self.client.get(reverse("memories.visits"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(pin, response.context["unlogged_visits"])

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("memories.visits"))
        self.assertEqual(response.status_code, 302)


class MemoriesUnloggedActionViewTests(TestCase):
    """MemoriesUnloggedActionView dismisses or un-marks a pin from the unlogged-visits queue."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")

    def test_dismiss_hides_pin_without_changing_visited_status(self) -> None:
        response = self.client.post(reverse("memories.unlogged.action", args=[self.pin.slug, "dismiss"]))

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertTrue(self.pin.unlogged_visit_dismissed)
        self.assertIsNotNone(self.pin.last_visited)
        self.assertNotIn(self.pin, unlogged_visited_pins(self.profile))

    def test_unmark_clears_last_visited_and_drops_pin_from_queue(self) -> None:
        response = self.client.post(reverse("memories.unlogged.action", args=[self.pin.slug, "unmark"]))

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.last_visited)
        self.assertNotIn(self.pin, unlogged_visited_pins(self.profile))

    def test_unmark_removes_visited_badge(self) -> None:
        # Every profile gets exactly one "Visited" status badge auto-created on
        # signup (see badges.signals.create_default_tags) - fetch that one rather
        # than baking a duplicate, so removal targets the badge actually attached.
        badge = Badge.objects.get(profile=self.profile, kind="status", name="Visited")
        self.pin.badges.add(badge)

        self.client.post(reverse("memories.unlogged.action", args=[self.pin.slug, "unmark"]))

        self.assertNotIn(badge, self.pin.badges.all())

    def test_unknown_action_is_404(self) -> None:
        response = self.client.post(reverse("memories.unlogged.action", args=[self.pin.slug, "bogus"]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_act_on_another_users_pin(self) -> None:
        other = baker.make(User)
        other_pin = _make_pin(other.profile, last_visited=_aware(2024, 6, 1), name="Their Secret Spot")

        response = self.client.post(reverse("memories.unlogged.action", args=[other_pin.slug, "dismiss"]))

        self.assertEqual(response.status_code, 404)
        other_pin.refresh_from_db()
        self.assertFalse(other_pin.unlogged_visit_dismissed)
