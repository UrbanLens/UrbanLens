"""Whether two people share a pinned place is one row of evidence, not both of their pin lists.

``ANYTHING_IN_COMMON`` is the default on both sides of the photo gate, so every gallery with another uploader in it
asks "do these two share a place". It used to answer by reading every location the viewer had pinned and every one
the uploader had, twice, and intersecting them in Python: 17,720 rows for one of the capacity population's accounts
before it had looked at a single photo. It also asked a narrower question than the photo page's own gate, which counts
two pins on one parcel as one place.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import resolution

from .test_places_campus import make_place, square

MORE_PINS = 10


class PhotoVisibilityDoesNotReadEveryPinTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.uploader = baker.make(User).profile
        self.placed = 0
        shared = Location.objects.create(latitude=30.0, longitude=40.0)
        self.viewer_pin = baker.make(Pin, profile=self.viewer, location=shared)
        self.uploader_pin = baker.make(Pin, profile=self.uploader, location=shared)
        self.image = baker.make(
            Image, profile=self.uploader, wiki=baker.make(Wiki, location=shared), pending_scan=False
        )

    def _pin(self, profile: Profile, count: int) -> None:
        for _ in range(count):
            self.placed += 1
            location = Location.objects.create(latitude=50 + self.placed * 0.01, longitude=60 + self.placed * 0.01)
            baker.make(Pin, profile=profile, location=location)

    def _visible(self) -> tuple[bool, int]:
        totals = [0]
        viewer = Profile.objects.get(pk=self.viewer.pk)
        with connection.execute_wrapper(_row_counting_wrapper(totals)):
            visible = Image.objects.filter(pk=self.image.pk).visible_to(viewer).exists()
        return visible, totals[0]

    def assertRowsDoNotGrowWithPinsOf(self, profile: Profile) -> None:
        visible, baseline = self._visible()
        self.assertTrue(visible, "a shared place should admit the photo, or the common-pin answer was never asked")
        self._pin(profile, MORE_PINS)
        visible, rows = self._visible()
        self.assertTrue(visible)
        self.assertEqual(rows, baseline, f"{MORE_PINS} more pins read {rows - baseline} more rows")

    def test_rows_read_do_not_grow_with_the_viewers_pins(self) -> None:
        self.assertRowsDoNotGrowWithPinsOf(self.viewer)

    def test_rows_read_do_not_grow_with_the_uploaders_pins(self) -> None:
        self.assertRowsDoNotGrowWithPinsOf(self.uploader)

    def test_without_a_shared_place_the_photo_stays_hidden(self) -> None:
        self._pin(self.uploader, MORE_PINS)
        self.uploader_pin.delete()

        visible, _ = self._visible()

        self.assertFalse(visible)

    def test_pins_on_one_parcel_at_different_coordinates_are_one_shared_place(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.003), name="Shared Parcel")
        wiki_spot = Location.objects.create(latitude=40.0, longitude=-74.0)
        mate_spot = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        for spot in (wiki_spot, mate_spot):
            resolution.resolve_location_place(spot)
        Image.objects.filter(pk=self.image.pk).update(wiki=baker.make(Wiki, location=wiki_spot, place=parcel))
        Pin.objects.filter(pk__in=(self.viewer_pin.pk, self.uploader_pin.pk)).delete()
        baker.make(Pin, profile=self.uploader, location=wiki_spot)
        baker.make(Pin, profile=self.viewer, location=mate_spot)
        self.assertTrue(
            Profile.visibility_permits(VisibilityChoice.ANYTHING_IN_COMMON, self.uploader, self.viewer),
            "the pair form should count one parcel as one place, or this compares nothing",
        )

        visible, _ = self._visible()

        self.assertTrue(visible)
