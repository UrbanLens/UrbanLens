"""A photo memory links to the page its photo is on, not the gallery fragment that page loads by htmx.

The fragment rendered on its own, without the site around it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.memories.aggregator import get_memory_events


class PhotoMemoryLinkTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location = Location.objects.create(latitude=42.5, longitude=-73.5)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Mill")
        self.wiki = Wiki.objects.create(location=self.location)
        now = timezone.localtime()
        # A year back to the day, which On This Day requires; 29 February has no such day.
        self.taken = (
            now.replace(year=now.year - 1) if (now.month, now.day) != (2, 29) else now.replace(year=now.year - 4)
        )

    def _photo(self, **owner: object) -> Image:
        return baker.make(
            Image,
            profile=self.profile,
            latitude=42.5,
            longitude=-73.5,
            taken_at=self.taken,
            pending_scan=False,
            **owner,
        )

    def _event_url(self) -> str:
        day = self.taken.date()
        events = [e for e in get_memory_events(self.profile, day, day) if e.type == "photo"]
        self.assertEqual(len(events), 1)
        return events[0].url

    def test_a_pin_photo_opens_the_pin(self) -> None:
        self._photo(pin=self.pin)
        self.assertEqual(self._event_url(), reverse("pin.details", args=[self.pin.slug]))

    def test_a_wiki_photo_opens_the_wiki(self) -> None:
        self._photo(wiki=self.wiki)
        self.assertEqual(self._event_url(), reverse("location.wiki", args=[self.location.slug]))

    def test_on_this_day_links_the_same_way(self) -> None:
        self._photo(pin=self.pin)
        response = self.client.get(reverse("memories.on_this_day"))
        self.assertContains(response, f'href="{reverse("pin.details", args=[self.pin.slug])}"')
        self.assertNotContains(response, reverse("pin.gallery", args=[self.pin.slug]))
