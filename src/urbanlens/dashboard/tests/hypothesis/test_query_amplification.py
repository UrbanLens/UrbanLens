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
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
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
    """The Private Pin page against its own content - labels, images, comments, visits."""

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


class WikiPageAmplificationTests(_AmplificationTestCase):
    """The community wiki page against its own content.

    The viewer needs a pin at the location: wiki visibility is gated on discovery
    (``location_visible_to``), so without one every request here would 404 and the
    measurement would be of an error page.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
        self.location = self._next_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        self.wiki = baker.make(Wiki, location=self.location, name="Powerhouse")
        self.url = reverse("location.wiki", kwargs={"location_slug": self.location.slug})

    def _measure(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_page_is_flat_in_comment_count(self) -> None:
        def add_comment() -> None:
            baker.make(Comment, wiki=self.wiki, profile=baker.make(User).profile, text="Looks abandoned.")

        self.assert_flat(add_comment, self._measure)

    def test_the_page_is_flat_in_alias_count(self) -> None:
        counter = {"n": 0}

        def add_alias() -> None:
            counter["n"] += 1
            baker.make(WikiAlias, wiki=self.wiki, name=f"Alias {counter['n']}", created_by=self.profile)

        self.assert_flat(add_alias, self._measure)

    def test_the_page_is_flat_in_link_count(self) -> None:
        counter = {"n": 0}

        def add_link() -> None:
            counter["n"] += 1
            baker.make(WikiLink, wiki=self.wiki, name=f"Link {counter['n']}", url=f"https://example.test/{counter['n']}", created_by=self.profile)

        self.assert_flat(add_link, self._measure)

    def test_the_page_is_flat_in_edit_history_count(self) -> None:
        def add_edit() -> None:
            baker.make(WikiEdit, wiki=self.wiki, editor=baker.make(User).profile, changes={"name": {"from": "a", "to": "b"}})

        self.assert_flat(add_edit, self._measure)


class TripDetailAmplificationTests(_AmplificationTestCase):
    """The trip detail page against its itinerary and roster."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
        self.trip = baker.make(Trip, creator=self.profile, allow_edit_activities="everyone", allow_add_activities="everyone")
        self.trip.profiles.add(self.profile)
        self.url = reverse("trips.detail", kwargs={"trip_slug": self.trip.slug})

    def _measure(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_page_is_flat_in_activity_count(self) -> None:
        counter = {"n": 0}

        def add_activity() -> None:
            counter["n"] += 1
            baker.make(TripActivity, trip=self.trip, title=f"Stop {counter['n']}", location=self._next_location(), order=counter["n"])

        self.assert_flat(add_activity, self._measure)

    def test_the_page_is_flat_in_member_count(self) -> None:
        def add_member() -> None:
            self.trip.profiles.add(baker.make(User).profile)

        self.assert_flat(add_member, self._measure)


class ExternalApiListAmplificationTests(_AmplificationTestCase):
    """The API serializes the same objects through a different path than the HTML
    pages, so it can carry its own amplification even where the page is clean."""

    def setUp(self) -> None:
        super().setUp()
        key, self.raw_key = generate_api_key(self.user, "Amplification test")
        ApiKey.objects.filter(pk=key.pk).update(scopes=[ApiKeyScope.PINS_READ.value, ApiKeyScope.TRIPS_READ.value])

    def _get(self, url: str) -> None:
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(response.status_code, 200)

    def test_the_pins_list_is_flat_in_pin_count(self) -> None:
        url = reverse("external_api:pins")

        def add_pin() -> None:
            pin = baker.make(Pin, profile=self.profile, location=self._next_location())
            pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))

        self.assert_flat(add_pin, lambda: self._get(url))

    def test_the_trips_list_is_flat_in_trip_count(self) -> None:
        url = reverse("external_api:trips")

        def add_trip() -> None:
            trip = baker.make(Trip, creator=self.profile)
            trip.profiles.add(self.profile)
            baker.make(TripActivity, trip=trip, location=self._next_location())

        self.assert_flat(add_trip, lambda: self._get(url))
