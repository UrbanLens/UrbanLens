"""Query-count regression guards for the highest-traffic pages.

Amplification has been found three times in this codebase - the notification
dropdown's reverse-OneToOne reads, the Memories trip source re-deriving each
trip's dates, SpotGuessr re-running eligibility per retry attempt - and every
one was invisible to inspection and obvious to a counter. So these assert the
shape rather than a magic number: build N items, count, add one more, count
again, and require the delta to be zero.

A page costing a fixed 40 queries is not an N+1 and is not what these catch;
they only fail when cost scales with content.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService


class _AmplificationTestCase(TestCase):
    """Shared helper: assert a callable's query count doesn't grow with its input."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self._coord = 0

    def _next_location(self) -> Location:
        """Locations are unique on (latitude, longitude) - hand out distinct points."""
        self._coord += 1
        return Location.objects.create(latitude=40.0 + self._coord / 10000, longitude=-74.0 - self._coord / 10000)

    def assert_flat(self, build_one, measure, *, baseline: int = 3, extra: int = 3) -> None:
        """Assert the query count does not grow when ``extra`` more items are added.

        A warm-up call is measured and discarded first: the first request of a test
        populates per-process caches (the SiteSettings memo, session and permission
        lookups), so comparing against it reports a *decrease* and tells you nothing
        about scaling. Growth is what matters, so the assertion is one-sided - a page
        getting cheaper is never the bug being hunted here.
        """
        for _ in range(baseline):
            build_one()
        measure()  # warm-up, deliberately not counted

        with CaptureQueriesContext(connection) as first:
            measure()

        for _ in range(extra):
            build_one()
        with CaptureQueriesContext(connection) as second:
            measure()

        before, after = len(first.captured_queries), len(second.captured_queries)
        self.assertLessEqual(
            after,
            before,
            f"query count grew from {before} to {after} for {extra} more items "
            f"({(after - before) / extra:.1f} per item); last queries:\n"
            + "\n".join(q["sql"][:160] for q in second.captured_queries[-6:]),
        )


class MapPinPayloadAmplificationTests(_AmplificationTestCase):
    """The map's pin payload - the single highest-traffic serialization in the app."""

    def _pin_with_labels(self) -> Pin:
        pin = baker.make(Pin, profile=self.profile, location=self._next_location())
        pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))
        pin.labels.add(baker.make(Label, profile=self.profile, kind="category"))
        return pin

    def test_serializing_the_map_payload_is_flat_in_pin_count(self) -> None:
        service = MapPinPayloadService(self.profile)

        def measure() -> None:
            # all() applies prepare_queryset itself - passing an already-prepared
            # queryset collides the labels prefetch and raises at evaluation.
            service.all(Pin.objects.filter(profile=self.profile))

        self.assert_flat(self._pin_with_labels, measure)


class PinDetailPageAmplificationTests(_AmplificationTestCase):
    """The pin detail page against its own content - labels, images, comments, visits."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=self._next_location())

    def test_the_page_is_flat_in_label_count(self) -> None:
        from django.urls import reverse

        url = reverse("pin.details", kwargs={"pin_slug": self.pin.slug})

        def add_label() -> None:
            self.pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))

        def measure() -> None:
            self.assertEqual(self.client.get(url).status_code, 200)

        self.assert_flat(add_label, measure)

    def test_the_page_is_flat_in_visit_count(self) -> None:
        from django.urls import reverse

        from urbanlens.dashboard.models.visits.model import PinVisit

        url = reverse("pin.details", kwargs={"pin_slug": self.pin.slug})

        def add_visit() -> None:
            baker.make(PinVisit, pin=self.pin)

        def measure() -> None:
            self.assertEqual(self.client.get(url).status_code, 200)

        self.assert_flat(add_visit, measure)
