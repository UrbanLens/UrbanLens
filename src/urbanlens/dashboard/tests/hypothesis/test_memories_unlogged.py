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
import json

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
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

    def test_pin_with_visited_label_but_no_record_is_included(self) -> None:
        pin = _make_pin(self.profile, last_visited=None)
        label = baker.make("dashboard.Label", profile=self.profile, kind="status", name="Visited")
        pin.labels.add(label)
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
        self.assertContains(response, "My Place")
        self.assertContains(response, "memories-unlogged-grid")
        self.assertContains(response, reverse("memories.visits"))

    def test_lists_unlogged_pins_shows_the_shared_select_map(self) -> None:
        """Reuses the same map/selection UX as Memories > Locations - see pin-select-map.js."""
        _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")
        response = self.client.get(reverse("memories.visits"))
        self.assertContains(response, 'id="unlogged-visits-map"')
        self.assertContains(response, "pin-select-map")
        self.assertContains(response, reverse("memories.visits.map_data"))
        self.assertContains(response, "ul-bulk-bar-unlogged_visits")

    def test_empty_queue_shows_caught_up_body(self) -> None:
        response = self.client.get(reverse("memories.visits"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["unlogged_visits"]), [])
        self.assertContains(response, "No visit details need your attention")
        self.assertContains(response, "View your timeline")
        self.assertNotContains(response, "memories-unlogged-grid")
        self.assertNotContains(response, reverse("memories.visits"))

    def test_empty_queue_has_no_map(self) -> None:
        response = self.client.get(reverse("memories.visits"))
        self.assertNotContains(response, 'id="unlogged-visits-map"')

    def test_map_uses_the_shared_toolbar_not_the_bespoke_pill_button(self) -> None:
        """Regression guard: the old .pin-select-toggle pill had its own unstyled
        top-right button and never disabled Leaflet's on-map attribution control.
        Replaced with the same {% map_toolbar %} component every other map uses."""
        _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")
        response = self.client.get(reverse("memories.visits"))
        self.assertContains(response, 'id="unlogged-visits-select-toggle"')
        self.assertContains(response, "map-btn-icon")
        self.assertContains(response, 'id="unlogged-visits-map-buttons"')
        self.assertNotContains(response, "pin-select-toggle")

    def test_map_page_enables_footer_attribution(self) -> None:
        """show_map_footer must be set whenever the map itself renders, so
        pin-select-map.js's onAttribution callback has somewhere to write to."""
        _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")
        response = self.client.get(reverse("memories.visits"))
        self.assertTrue(response.context["show_map_footer"])
        self.assertContains(response, "page-footer--map")

    def test_empty_queue_does_not_enable_footer_attribution(self) -> None:
        response = self.client.get(reverse("memories.visits"))
        self.assertFalse(response.context["show_map_footer"])

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

    def test_unmark_removes_visited_label(self) -> None:
        # Every profile gets exactly one "Visited" status label auto-created on
        # signup (see labels.signals.create_default_tags) - fetch that one rather
        # than baking a duplicate, so removal targets the label actually attached.
        label = Label.objects.get(profile=self.profile, kind="status", name="Visited")
        self.pin.labels.add(label)

        self.client.post(reverse("memories.unlogged.action", args=[self.pin.slug, "unmark"]))

        self.assertNotIn(label, self.pin.labels.all())

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


class MemoriesVisitsMapDataViewTests(TestCase):
    """MemoriesVisitsMapDataView - JSON for the Visits page's shared select-map."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_returns_coordinates_and_name_for_unlogged_pins(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1), name="My Place")
        response = self.client.get(reverse("memories.visits.map_data"))
        self.assertEqual(response.status_code, 200)
        data = response.json()["pins"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], pin.slug)
        self.assertEqual(data[0]["name"], "My Place")
        self.assertEqual(data[0]["latitude"], pin.effective_latitude)
        self.assertEqual(data[0]["longitude"], pin.effective_longitude)
        self.assertEqual(data[0]["last_visited"], "2024-06-01")

    def test_excludes_pins_with_a_logged_visit(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), source=VisitSource.MANUAL)
        response = self.client.get(reverse("memories.visits.map_data"))
        self.assertEqual(response.json()["pins"], [])

    def test_scoped_to_the_requesting_profile(self) -> None:
        other = baker.make(User)
        _make_pin(other.profile, last_visited=_aware(2024, 6, 1), name="Not Mine")
        response = self.client.get(reverse("memories.visits.map_data"))
        self.assertEqual(response.json()["pins"], [])

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("memories.visits.map_data"))
        self.assertEqual(response.status_code, 302)


class MemoriesVisitsBulkActionViewTests(TestCase):
    """MemoriesVisitsBulkActionView - bulk quick-log/unmark, owned+unlogged only."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, action: str, slugs: list[str]):
        return self.client.post(reverse("memories.visits.bulk", args=[action]), data=json.dumps({"pin_slugs": slugs}), content_type="application/json")

    def test_log_creates_a_dated_visit_for_each_pin(self) -> None:
        first = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        second = _make_pin(self.profile, last_visited=_aware(2024, 6, 2))
        response = self._post("log", [first.slug, second.slug])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed"], 2)
        self.assertTrue(PinVisit.objects.filter(pin=first).exists())
        self.assertTrue(PinVisit.objects.filter(pin=second).exists())
        self.assertNotIn(first, unlogged_visited_pins(self.profile))

    def test_log_visit_is_dated_today(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        self._post("log", [pin.slug])
        visit = PinVisit.objects.get(pin=pin)
        self.assertEqual(visit.visited_at.date(), timezone.now().date())

    def test_log_respects_visit_logging_disabled(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        response = self._post("log", [pin.slug])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PinVisit.objects.filter(pin=pin).exists())

    def test_unmark_clears_visited_status_for_each_pin(self) -> None:
        first = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        second = _make_pin(self.profile, last_visited=_aware(2024, 6, 2))
        response = self._post("unmark", [first.slug, second.slug])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed"], 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.last_visited)
        self.assertIsNone(second.last_visited)

    def test_skips_another_profiles_pin(self) -> None:
        other = baker.make(User)
        foreign = _make_pin(other.profile, last_visited=_aware(2024, 6, 1))
        mine = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        response = self._post("unmark", [foreign.slug, mine.slug])
        self.assertEqual(response.json()["processed"], 1)
        foreign.refresh_from_db()
        self.assertIsNotNone(foreign.last_visited)

    def test_skips_already_logged_pin(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), source=VisitSource.MANUAL)
        response = self._post("log", [pin.slug])
        self.assertEqual(response.json()["processed"], 0)

    def test_empty_pin_slugs_is_400(self) -> None:
        response = self._post("log", [])
        self.assertEqual(response.status_code, 400)

    def test_unknown_action_is_404(self) -> None:
        pin = _make_pin(self.profile, last_visited=_aware(2024, 6, 1))
        response = self.client.post(reverse("memories.visits.bulk", args=["explode"]), data=json.dumps({"pin_slugs": [pin.slug]}), content_type="application/json")
        self.assertEqual(response.status_code, 404)
