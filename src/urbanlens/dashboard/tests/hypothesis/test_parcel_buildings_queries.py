"""The pin page's Buildings card, and its API twin, cost the same queries for 3 child pins as for 12.

Each unnamed child pin was named by its location's wiki, read once per pin: dev's 517-child campus pin answered the
card in 99 queries, 82 of them that read.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.tests.hypothesis.test_parcel_buildings import _REDATA_BUILDINGS


class BuildingsCardQueryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude=41.7331, longitude=-73.9301, google_place=None)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="zz-card-campus")
        LocationCache.set(
            self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _REDATA_BUILDINGS, "provider": "redata"}
        )

    def _add_unnamed_children(self, count: int, parent: Pin | None = None) -> list[Pin]:
        made = []
        for _ in range(count):
            offset = Pin.objects.count() * 0.0007
            location = baker.make(Location, latitude=41.6 + offset, longitude=-73.8 - offset, google_place=None)
            child = baker.make(
                Pin,
                profile=self.user.profile,
                parent_pin=parent or self.pin,
                pin_type=PinType.BUILDING,
                name="",
                location=location,
            )
            baker.make(Wiki, location=location, name=f"zz-card-wiki-{child.pk}")
            made.append(child)
        return made

    def _card_queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("pin.parcel_buildings", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def _api_queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            ParcelBuildingsPanelSource().api_payload(Pin.objects.get(pk=self.pin.pk))
        return len(captured)

    def test_the_card_does_not_read_each_childs_wiki(self) -> None:
        nested_under = self._add_unnamed_children(3)
        self._add_unnamed_children(3, parent=nested_under[0])
        few = self._card_queries()
        self._add_unnamed_children(9)
        self._add_unnamed_children(9, parent=nested_under[0])

        self.assertEqual(self._card_queries(), few)

    def test_the_api_payload_does_not_read_each_childs_wiki(self) -> None:
        self._add_unnamed_children(3)
        few = self._api_queries()
        self._add_unnamed_children(9)

        self.assertEqual(self._api_queries(), few)

    def test_an_unnamed_child_is_listed_by_its_wiki_name(self) -> None:
        (child,) = self._add_unnamed_children(1)

        self.assertContains(
            self.client.get(reverse("pin.parcel_buildings", kwargs={"pin_slug": self.pin.slug})),
            f"zz-card-wiki-{child.pk}",
        )
