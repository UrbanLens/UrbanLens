"""The photo map layer is bounded, and says so when it had to be.

`PinGalleryJsonView` and `WikiGalleryJsonView` plotted every geotagged photo,
built a dict per row, and resolved each uploader's visibility while doing it -
auto-fetched on page load, so a popular location cost that on every visit.

Two properties here, and the second is the one that makes the cap acceptable
rather than merely cheap: the response admits it is partial, so the client can
say so instead of quietly showing half a site.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.geo import sampling

#: Small enough to seed quickly, large enough that the grid has work to do.
_TEST_CAP = 4


class ThePinPhotoMapLayerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude="40.0", longitude="-75.0")
        )

    def _photo(self, lat: float, lon: float) -> Image:
        return baker.make(Image, pin=self.pin, profile=self.profile, latitude=lat, longitude=lon, map_hidden=False)

    def _layer(self) -> dict:
        response = self.client.get(reverse("pin.gallery.json", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_a_small_gallery_is_not_capped(self) -> None:
        for index in range(3):
            self._photo(40.0 + index / 1000, -75.0)
        body = self._layer()

        self.assertEqual(len(body["images"]), 3)
        self.assertFalse(body["truncated"], "a gallery under the cap was reported as truncated")
        self.assertEqual(body["total"], 3)

    def test_a_large_gallery_is_capped_and_says_so(self) -> None:
        for index in range(12):
            self._photo(40.0 + index / 10_000, -75.0 + index / 10_000)

        with mock.patch.object(sampling, "MAX_MAP_PHOTOS", _TEST_CAP):
            body = self._layer()

        self.assertEqual(len(body["images"]), _TEST_CAP)
        self.assertTrue(body["truncated"], "the layer dropped photos without admitting it")
        self.assertEqual(body["total"], 12, "the client cannot say how many were left out")

    def test_the_kept_photos_still_cover_the_area(self) -> None:
        """A slice would keep twelve doorway photos and drop the outlier."""
        for index in range(12):
            self._photo(40.0 + index / 1_000_000, -75.0 + index / 1_000_000)
        outlier = self._photo(41.5, -74.0)

        with mock.patch.object(sampling, "MAX_MAP_PHOTOS", _TEST_CAP):
            body = self._layer()

        self.assertIn(
            outlier.pk,
            [img["id"] for img in body["images"]],
            "the one distant photo was dropped for more of the same spot",
        )

    def test_the_cap_under_test_is_the_one_the_view_reads(self) -> None:
        """Guards the override itself. `bound_map_layer` moved into the geo
        service, and a patch aimed at the old module would configure a ceiling
        nothing reads - which is how a capping test comes to pass against
        uncapped code."""
        for index in range(6):
            self._photo(40.0 + index / 10_000, -75.0 + index / 10_000)

        with mock.patch.object(sampling, "MAX_MAP_PHOTOS", 2):
            self.assertEqual(len(self._layer()["images"]), 2)

    def test_a_photo_without_coordinates_is_not_plotted(self) -> None:
        """Non-vacuity on the filter the cap sits behind."""
        self._photo(40.0, -75.0)
        baker.make(Image, pin=self.pin, profile=self.profile, latitude=None, longitude=None)
        body = self._layer()

        self.assertEqual(body["total"], 1)


class TheAlbumMapTests(TestCase):
    """The album grid pages at 48; its map used to embed the whole album.

    So pagination bounded what you could see and nothing bounded what was sent -
    a 5,000-photo album shipped 48 cards and 5,000 map entries in one response.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude="40.0", longitude="-75.0")
        )

    def _album_with(self, count: int):  # noqa: ANN202
        from urbanlens.dashboard.models.album.model import Album, AlbumItem

        album = baker.make(Album, parent_pin=self.pin, profile=self.profile)
        for index in range(count):
            image = baker.make(
                Image,
                pin=self.pin,
                profile=self.profile,
                latitude=40.0 + index / 10_000,
                longitude=-75.0 + index / 10_000,
                map_hidden=False,
            )
            baker.make(AlbumItem, album=album, image=image)
        return album

    def test_the_album_map_is_capped_and_says_so(self) -> None:
        album = self._album_with(10)
        with mock.patch.object(sampling, "MAX_MAP_PHOTOS", 3):
            response = self.client.get(
                reverse("pin.albums.detail", kwargs={"pin_slug": self.pin.slug, "album_slug": album.slug}),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["map_truncated"], "the album map dropped photos without saying so")
        self.assertEqual(len(response.context["map_photos"]), 3)
        self.assertEqual(response.context["map_total"], 10)

    def test_a_small_album_is_not_capped(self) -> None:
        album = self._album_with(4)
        response = self.client.get(
            reverse("pin.albums.detail", kwargs={"pin_slug": self.pin.slug, "album_slug": album.slug}),
        )

        self.assertFalse(response.context["map_truncated"])
        self.assertEqual(len(response.context["map_photos"]), 4)
