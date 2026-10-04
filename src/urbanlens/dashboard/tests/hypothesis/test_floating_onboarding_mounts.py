"""P211: a page's floating onboarding card is drawn above the page hero, not beneath it.

`.container` is a stacking context (`position: relative; z-index: 1`), so a fixed panel inside it can never rise
above a hero's own positioned elements, such as a wiki's "Community wiki" notice. The mounts live outside it.
"""

from __future__ import annotations

from html.parser import HTMLParser

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Ancestors(HTMLParser):
    """The classes of every element enclosing the element with ``target_id``."""

    def __init__(self, target_id: str) -> None:
        super().__init__()
        self.target_id = target_id
        self.stack: list[tuple[str, str]] = []
        self.found: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id") == self.target_id and self.found is None:
            self.found = [classes for _tag, classes in self.stack]
        if tag not in _VOID:
            self.stack.append((tag, values.get("class") or ""))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _ancestor_classes(html: str, target_id: str) -> list[str]:
    parser = _Ancestors(target_id)
    parser.feed(html)
    assert parser.found is not None, f"#{target_id} is not on the page"
    return parser.found


class FloatingOnboardingMountTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude="41.4", longitude="-73.4")
        baker.make(Wiki, location=self.location, name="Old Mill")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)

    def assert_outside_the_container(self, url: str, mount: str) -> None:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        enclosing = _ancestor_classes(response.content.decode(), mount)
        self.assertEqual([classes for classes in enclosing if "container" in classes.split()], [])

    def test_the_wiki_page(self) -> None:
        self.assert_outside_the_container(reverse("location.wiki", args=[self.location.slug]), "wiki-onboarding")

    def test_the_pin_page(self) -> None:
        self.assert_outside_the_container(reverse("pin.details", args=[self.pin.slug]), "pin-detail-onboarding")

    def test_the_trip_page(self) -> None:
        trip = Trip.objects.create(name="Weekend", creator=self.user.profile)
        TripMembership.objects.get_or_create(trip=trip, profile=self.user.profile, defaults={"rsvp": "yes"})
        self.assert_outside_the_container(reverse("trips.detail", kwargs={"trip_slug": trip.slug}), "trip-onboarding")

    def test_the_organize_page(self) -> None:
        self.assert_outside_the_container(reverse("organize.index"), "organize-onboarding")
