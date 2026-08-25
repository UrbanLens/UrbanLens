"""Every Memories page, requested as a real HTTP GET by a logged-in seeded demo account.

Model-level correctness (test_demo_memories_content.py) proves the data
exists in the right shape; it does not prove the view/template layer can
actually render it. This is the closer proof: if a page 500s here, something
in this batch of seeded content is genuinely wrong, not just untested.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.demo.seeding import seed_demo_account


class DemoMemoriesPagesRenderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        locations = [baker.make(Location, google_place=None) for _ in range(15)]
        for location in locations:
            Wiki.objects.create(location=location, name=location.official_name or "Wiki")
        with mock.patch("urbanlens.dashboard.services.demo.seeding.pool_locations", return_value=locations):
            self.owner_user = seed_demo_account()
        self.client.force_login(self.owner_user)

    def _assert_ok(self, url_name: str) -> None:
        response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 200, f"{url_name} returned {response.status_code}")

    def test_the_memories_index_renders(self) -> None:
        self._assert_ok("memories.view")

    def test_on_this_day_renders(self) -> None:
        self._assert_ok("memories.on_this_day")

    def test_hero_stats_renders(self) -> None:
        self._assert_ok("memories.hero_stats")

    def test_the_unlogged_visits_queue_renders(self) -> None:
        self._assert_ok("memories.visits")

    def test_the_maps_page_renders(self) -> None:
        self._assert_ok("memories.maps")

    def test_the_sharing_page_renders(self) -> None:
        self._assert_ok("memories.sharing")

    def test_the_journal_page_renders(self) -> None:
        self._assert_ok("memories.journal")

    def test_the_photos_page_renders(self) -> None:
        self._assert_ok("memories.photos")
