"""One failing memory source must not take the whole Memories feed down.

``get_memory_events`` merges four independent sources - recorded routes, trips,
visits and photos - and is explicitly an extensibility seam: its module docstring
says adding a memory type is one new function appended to ``_EVENT_SOURCES`` and
nothing else changes. That is exactly what makes unguarded fan-out costly here:
any one source raising (a corrupt row, a missing relation, a geometry error, or a
bug in a newly added source) discarded the other three and returned a 500 for the
page, on both the HTML feed and the external API.

Same shape as the site-admin status page, which 500'd whenever one infrastructure
service was unreachable - and the same fix: isolate each contributor, so a feed
that is missing one kind of memory still shows the rest.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.memories import aggregator

_PHOTOS = "urbanlens.dashboard.services.memories.aggregator._photos_for_range"
_VISITS = "urbanlens.dashboard.services.memories.aggregator._visits_for_range"


class MemorySourceIsolationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Powerhouse")
        self.visited_at = timezone.now() - timedelta(days=2)
        baker.make(PinVisit, pin=self.pin, visited_at=self.visited_at)
        self.start = (timezone.now() - timedelta(days=10)).date()
        self.end = timezone.now().date()

    def _events(self):
        return aggregator.get_memory_events(self.profile, self.start, self.end)

    def test_the_visit_shows_up_normally(self) -> None:
        """Baseline - without this the isolation tests prove nothing."""
        self.assertIn("visit", {event.type for event in self._events()})

    def test_one_broken_source_does_not_lose_the_others(self) -> None:
        with mock.patch(_PHOTOS, side_effect=ValueError("corrupt exif row")):
            events = self._events()

        self.assertIn("visit", {event.type for event in events}, "a healthy source must still contribute")

    def test_every_source_failing_yields_an_empty_feed_rather_than_an_error(self) -> None:
        sources = (
            "_routes_for_range",
            "_trips_for_range",
            "_visits_for_range",
            "_photos_for_range",
        )
        with mock.patch.multiple(
            aggregator,
            **{name: mock.Mock(side_effect=RuntimeError("boom")) for name in sources},
        ):
            self.assertEqual(self._events(), [])

    def test_a_source_that_fails_midway_keeps_what_it_already_yielded(self) -> None:
        """The sources are generators - a failure partway through must not discard
        the events it had already produced, nor the other sources' events."""

        def half_broken(profile, start, end, bbox):
            yield aggregator.MemoryEvent(
                type="photo",
                occurred_at=aggregator._date_to_datetime(self.start),  # noqa: SLF001 - shared helper
                ended_at=None,
                title="First photo",
                subtitle="Photo",
                latitude=None,
                longitude=None,
                url="/x/",
                thumbnail_url=None,
                icon="photo",
                color="#000",
                extra={},
            )
            raise ValueError("second row is corrupt")

        with mock.patch(_PHOTOS, side_effect=half_broken):
            events = self._events()

        titles = {event.title for event in events}
        self.assertIn("First photo", titles, "events yielded before the failure must survive")
        self.assertIn("visit", {event.type for event in events}, "other sources must still contribute")
